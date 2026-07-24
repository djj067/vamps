"""
VAMPS · BuildPrototype — Langflow 1.7.3 custom component.

Port of the FastAPI `/build` endpoint, simplified to a BLOCKING call (the
component runs the Claude Code CLI with subprocess.run and waits for it to
finish, instead of the original background-job + polling machinery).

Session resume is preserved: a module-level registry maps a project_id to the
last successful Claude Code session, so a rebuild resumes that session and edits
the existing index.html in place rather than regenerating from scratch. The
registry lives for the life of the Langflow server process — same lifetime the
original in-memory dict had.

The generated index.html is returned both as text (for direct rendering) and as
a path on disk. Langflow does not serve arbitrary static files, so whatever UI
drives the flow is responsible for displaying/hosting the returned HTML.
"""
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

# Flat top-level imports required by Langflow's flow loader (see spec_generator.py).
from langflow.custom import Component
from langflow.io import BoolInput, IntInput, MultilineInput, Output, StrInput
from langflow.schema.data import Data
from langflow.schema.message import Message

# Session state is persisted to a small file inside each project's directory
# (see _SESSION_FILE). A module-level dict would NOT work here: Langflow re-exec's
# the component code per run, so module globals reset — and annotated module-level
# assignments are dropped by the flow loader entirely. A file survives both.
_SESSION_FILE = ".vamps_session.json"


BUILD_PROMPT = """Read SPEC.md in this directory. Build a single, self-contained
clickable PROTOTYPE in ONE file named index.html.

Requirements:
- Everything inline in index.html: HTML + CSS + JavaScript. No external files,
  no CDNs, no build step, no network calls.
- Use realistic placeholder/mock data so the key screens and flows from the spec
  are demonstrable by clicking around. This is a visual prototype, not a real
  backend.
- Keep it clean and simple — the goal is to show something at the end of a
  meeting, not a finished product.
- Create ONLY index.html. Do not create other files or run any commands."""

BUILD_PROMPT_RESUME = """The functional specification in SPEC.md has been revised.
UPDATE the existing index.html in this directory so it matches the revised spec.
Change only what needs to change to reflect the update — keep the rest of the
prototype intact. Edit index.html in place; do NOT rewrite it from scratch and do
NOT create other files."""

_TOOL_FLAGS = ["--permission-mode", "acceptEdits",
               "--allowed-tools", "Write", "Edit", "Read", "MultiEdit"]


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", s)[:40]


def _build_tokens(stdout_text: str):
    """Pull Claude Code token usage from its --output-format json result."""
    try:
        data = json.loads(stdout_text)
    except Exception:
        return None
    u = data.get("usage") or {}
    inp = (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0) \
        + (u.get("cache_creation_input_tokens", 0) or 0)
    out = u.get("output_tokens", 0) or 0
    return {"input": inp, "output": out, "total": inp + out, "cost_usd": data.get("total_cost_usd")}


class BuildPrototype(Component):
    display_name = "VAMPS Build Prototype"
    description = "Hand a spec to the Claude Code CLI (blocking) to generate/edit a single-file HTML prototype."
    documentation = "https://github.com/djj067/vamps"
    icon = "hammer"
    name = "vamps_build_prototype"

    inputs = [
        MultilineInput(
            name="spec",
            display_name="Spec (Markdown)",
            info="The functional spec to build a prototype from. Written to SPEC.md.",
            required=True,
        ),
        StrInput(
            name="project_id",
            display_name="Project ID",
            info="Stable id that ties rebuilds together for session resume. Blank = new random project each run.",
            value="",
        ),
        BoolInput(
            name="resume",
            display_name="Resume session (edit existing)",
            info="If a prior build for this project succeeded, resume it and edit the existing index.html.",
            value=True,
        ),
        StrInput(
            name="output_root",
            display_name="Output Root Dir",
            info="Directory under which each project's folder (with SPEC.md + index.html) is created.",
            value="generated",
        ),
        StrInput(
            name="claude_bin",
            display_name="Claude Code binary",
            value="claude",
            advanced=True,
        ),
        IntInput(
            name="timeout_s",
            display_name="Timeout (seconds)",
            value=300,
            advanced=True,
        ),
    ]

    outputs = [
        Output(name="html", display_name="index.html (text)", method="build_html"),
        Output(name="result", display_name="Build Result (Data)", method="build_result"),
        Output(name="result_json", display_name="Build Result (JSON text)", method="build_result_json"),
    ]

    @staticmethod
    def _read_session(out: Path):
        """Return the last successful Claude Code session id for this project, if any."""
        try:
            return json.loads((out / _SESSION_FILE).read_text(encoding="utf-8")).get("session_id")
        except Exception:
            return None

    @staticmethod
    def _write_session(out: Path, session_id: str) -> None:
        try:
            (out / _SESSION_FILE).write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
        except Exception:
            pass

    def _run(self) -> dict:
        if getattr(self, "_computed", None) is not None:
            return self._computed

        spec_md = (self.spec or "").strip()
        if not spec_md:
            raise ValueError("BuildPrototype: spec is empty.")

        claude_bin = shutil.which(self.claude_bin) or self.claude_bin
        project_id = _safe_id(str(self.project_id or "")) or uuid.uuid4().hex[:8]

        gen_root = Path(self.output_root or "generated")
        gen_root.mkdir(parents=True, exist_ok=True)
        out = gen_root / project_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "SPEC.md").write_text(spec_md, encoding="utf-8")

        index_path, backup_path = out / "index.html", out / ".index.backup.html"
        session_id = self._read_session(out)
        do_resume = bool(self.resume and session_id and index_path.exists())

        backup = None
        if do_resume:
            try:
                shutil.copyfile(index_path, backup_path)
                backup = backup_path
            except Exception:
                backup = None
            cmd = [claude_bin, "--resume", session_id, "-p", BUILD_PROMPT_RESUME,
                   "--output-format", "json", *_TOOL_FLAGS]
        else:
            cmd = [claude_bin, "-p", BUILD_PROMPT, "--output-format", "json", *_TOOL_FLAGS]

        self.log(f"Building prototype for project '{project_id}' (resume={do_resume})")
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=str(out), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=int(self.timeout_s),
            )
            rc, stdout_text, err_text = proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError:
            raise ValueError(f"Claude Code CLI '{self.claude_bin}' not found on PATH.")
        except subprocess.TimeoutExpired:
            rc, stdout_text, err_text = -1, "", f"timed out after {self.timeout_s}s"

        index_ready = index_path.exists()
        # Finalize: persist session on success, restore backup on failure.
        if rc == 0 and index_ready:
            try:
                sid = json.loads(stdout_text).get("session_id")
            except Exception:
                sid = None
            if sid:
                self._write_session(out, sid)   # future rebuilds resume from here
            if backup and backup.exists():
                backup.unlink()
        else:
            if backup and backup.exists():
                try:
                    shutil.copyfile(backup, index_path)
                    backup.unlink()
                except Exception:
                    pass
            index_ready = index_path.exists()

        html = ""
        if index_ready:
            html = index_path.read_text(encoding="utf-8", errors="replace")

        status = "done" if (rc == 0 and index_ready) else ("done_no_file" if rc == 0 else "error")
        self._computed = {
            "ok": rc == 0 and index_ready,
            "status": status,
            "project_id": project_id,
            "resumed": do_resume,
            "returncode": rc,
            "elapsed_s": round(time.time() - t0, 1),
            "index_path": str(index_path) if index_ready else None,
            "html": html,
            "tokens": _build_tokens(stdout_text),
            "log": (err_text or stdout_text)[-4000:],
        }
        return self._computed

    def build_html(self) -> Message:
        result = self._run()
        self.status = result["status"]
        return Message(text=result["html"])

    def build_result(self) -> Data:
        result = self._run()
        preview = dict(result)
        preview.pop("html", None)  # keep the status preview compact
        self.status = preview
        return Data(data=result)

    def build_result_json(self) -> Message:
        """Full result (incl. generated html) as a JSON string — for the run API."""
        result = self._run()
        return Message(text=json.dumps(result))
