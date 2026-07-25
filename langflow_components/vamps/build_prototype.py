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
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

# Flat top-level imports required by Langflow's flow loader (see spec_generator.py).
from langflow.custom import Component
from langflow.io import BoolInput, IntInput, MessageTextInput, MultilineInput, Output, SecretStrInput, StrInput
from langflow.schema.data import Data
from langflow.schema.message import Message
from openai import OpenAI  # OpenRouter (Claude) HTML-generation path

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

# --- OpenRouter (Option B): ask a Claude model for one self-contained index.html ---
# No Claude Code CLI, no tools, no session resume — a single chat completion whose
# reply IS the HTML document. Used when provider == "openrouter".
BUILD_SYSTEM_OR = """You are a senior front-end engineer. Given a functional
specification, produce a SINGLE, self-contained clickable prototype as ONE complete
index.html file:
- All HTML, CSS, and JavaScript inline. No external files, CDNs, build steps, or network calls.
- Use realistic placeholder/mock data so the key screens and flows are demonstrable by clicking.
- Keep it clean and simple — a visual prototype, not a finished product.
Output ONLY the raw HTML document, starting with <!doctype html>. Do NOT wrap it in
markdown code fences and do NOT add any commentary before or after."""

BUILD_USER_OR = """Functional specification:
---
{spec}
---
Return the complete index.html now."""

# Comment-command edit: apply a one-line change to the EXISTING prototype instead
# of regenerating from the spec. Used when an `instruction` is provided.
BUILD_EDIT_SYSTEM = """You are a senior front-end engineer editing an existing
single-file HTML prototype. Apply ONLY the requested change and return the COMPLETE
updated index.html. Keep everything else exactly as it was. All HTML/CSS/JS stays
inline — no external files, CDNs, or network calls. Output ONLY the raw HTML
document starting with <!doctype html> — no markdown fences, no commentary."""

BUILD_EDIT_USER = """CURRENT index.html:
---
{html}
---
CHANGE REQUESTED:
{instruction}

Return the full updated index.html now."""


def _strip_html(text: str) -> str:
    """Pull a clean HTML document out of a chat reply (drop code fences / preamble)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    low = t.lower()
    # Trim any preamble to the earliest HTML start marker, but keep a leading
    # <!doctype> (don't let the <html> marker chop it off).
    idxs = [low.find(m) for m in ("<!doctype", "<html") if low.find(m) != -1]
    if idxs and min(idxs) > 0:
        return t[min(idxs):]
    return t


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
        MultilineInput(
            name="instruction",
            display_name="Change instruction (optional)",
            info="A plain-language change to apply to the EXISTING index.html (comment-command). "
                 "When set (openrouter provider), the model edits the current HTML instead of "
                 "regenerating from the spec.",
            value="",
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
        MessageTextInput(
            name="provider",
            display_name="Provider",
            info="'claude_cli' (Claude Code CLI writes index.html) or 'openrouter' "
                 "(call a Claude model via OpenRouter and return the HTML directly, no CLI). "
                 "Blank = the BUILD_PROVIDER / SPEC_PROVIDER env var (default claude_cli).",
            value="",
        ),
        MessageTextInput(
            name="model",
            display_name="Model (OpenRouter)",
            info="OpenRouter model slug for the openrouter provider. "
                 "Blank = BUILD_MODEL env / anthropic/claude-sonnet-5.",
            value="",
        ),
        SecretStrInput(
            name="api_key",
            display_name="OpenRouter API Key",
            info="Only for the openrouter provider. Blank = OPENROUTER_API_KEY env var.",
            required=False,
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

    def _provider(self) -> str:
        return ((self.provider or "").strip()
                or os.getenv("BUILD_PROVIDER", "")
                or os.getenv("SPEC_PROVIDER", "claude_cli")).lower()

    def _build_via_openrouter(self, spec_md: str, project_id: str, out: Path) -> dict:
        """Option B: generate index.html by calling a Claude model via OpenRouter.

        A single chat completion returns the whole HTML document; we clean and write
        it to index.html so the webpage can serve it exactly like the CLI path. No
        Claude Code, so there's no session resume — every run regenerates from the spec.
        """
        key = (self.api_key or "").strip() or os.getenv("OPENROUTER_API_KEY", "")
        base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        model = (self.model or "").strip() or os.getenv("BUILD_MODEL", "anthropic/claude-sonnet-5")
        index_path = out / "index.html"

        # Comment-command: if an instruction is given and a prototype already exists,
        # EDIT the existing HTML instead of regenerating the whole thing from the spec.
        instruction = (getattr(self, "instruction", "") or "").strip()
        existing = ""
        if instruction and index_path.exists():
            existing = index_path.read_text(encoding="utf-8", errors="replace")
        if instruction and existing:
            messages = [{"role": "system", "content": BUILD_EDIT_SYSTEM},
                        {"role": "user", "content": BUILD_EDIT_USER.format(html=existing, instruction=instruction)}]
        else:
            messages = [{"role": "system", "content": BUILD_SYSTEM_OR},
                        {"role": "user", "content": BUILD_USER_OR.format(spec=spec_md)}]

        t0 = time.time()
        resp = OpenAI(base_url=base, api_key=key).chat.completions.create(
            model=model, temperature=0.4, messages=messages,
        )
        html = _strip_html(resp.choices[0].message.content or "")
        ok = "<" in html and len(html) > 30
        if ok:
            index_path.write_text(html, encoding="utf-8")

        u = getattr(resp, "usage", None)
        inp = getattr(u, "prompt_tokens", 0) or 0
        outp = getattr(u, "completion_tokens", 0) or 0
        return {
            "ok": ok,
            "status": "done" if ok else "error",
            "project_id": project_id,
            "resumed": False,
            "returncode": 0 if ok else 1,
            "elapsed_s": round(time.time() - t0, 1),
            "index_path": str(index_path) if ok else None,
            "html": html,
            "tokens": {"input": inp, "output": outp, "total": inp + outp, "cost_usd": None},
            "log": f"openrouter build via {model}" if ok else "openrouter build returned no usable HTML",
        }

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

        # Option B: openrouter provider generates the HTML by calling a Claude model
        # directly (no Claude Code CLI, no session resume) and returns it as text.
        if self._provider() == "openrouter":
            self._computed = self._build_via_openrouter(spec_md, project_id, out)
            return self._computed

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
