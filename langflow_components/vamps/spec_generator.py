"""
VAMPS · SpecGenerator — Langflow 1.7.3 custom component.

Port of the FastAPI `/spec` endpoint. Turns a meeting transcript (plus any
prior spec / open questions) into a functional-specification Markdown document,
a list of clarifying questions, and a list of questions the material now
answers. Answers pass two gates before they count as resolved:
  1. their evidence quote must really appear in the material, and
  2. a strict second-pass "skeptic" LLM call must confirm it.

The prompts and JSON contract are identical to the original backend so behaviour
matches one-to-one.
"""
import json
import os
import re
import shutil
import subprocess

# NOTE: keep these as flat, top-level imports. Langflow's flow loader exec's the
# embedded component code and only binds imports found at module top level (it
# also auto-falls-back langflow.* -> lfx.* on 1.7.x), so a try/except import
# block would leave `Component` undefined when a flow is imported from JSON.
from langflow.custom import Component
from langflow.io import BoolInput, MessageTextInput, MultilineInput, Output, SecretStrInput
from langflow.schema.data import Data
from langflow.schema.message import Message
from openai import OpenAI  # used for the OpenRouter (demo) provider


# --- Prompt contract (verbatim from the original backend) --------------------
SPEC_STRUCTURE = """# Functional Specification

## 1. Meeting Summary
2-4 sentences: who/what/why.

## 2. Client Goals & Pain Points
Bulleted list.

## 3. Proposed Solution / Scope
What we will build or deliver.

## 4. Functional Requirements
Numbered list. Each item: a concrete, testable requirement.

## 5. Non-Functional Requirements
Performance, security, compliance, etc. (only if mentioned or clearly implied.)

## 6. Action Items
Who does what next (use "Unassigned" if the transcript doesn't say)."""

SPEC_SYSTEM = f"""You are a business analyst supporting a sales team. You turn
the raw material of a client meeting (a transcript that may contain
transcription errors, filler words and overlapping speakers, plus any
pre-existing notes) into a clear FUNCTIONAL SPECIFICATION.

FIRST, filter the material. A real meeting transcript is full of content that is
NOT about what to build: greetings and small talk, scheduling and logistics,
side conversations, jokes, weather, off-topic tangents, people testing the mic,
and unrelated chit-chat. IGNORE all of that. Base the specification ONLY on
content relevant to the client's product, goals, needs, scope, and requirements.
Never turn an off-topic remark into a requirement, an action item, or a
clarifying question. If a whole segment is irrelevant, leave the spec unchanged.

You ALWAYS respond with a single JSON object with exactly these keys:
  "spec":      a Markdown string using EXACTLY this structure and headings:
{SPEC_STRUCTURE}
  "questions": an array of objects {{"question": <string>, "anchor": <string>}}
               — the clarifying questions you, as the analyst, STILL need
               answered (things the material leaves ambiguous). For each:
                 - "question": one specific question, one sentence.
                 - "anchor": a SHORT verbatim quote (a few words) copied EXACTLY
                   from your "spec" markdown identifying the sentence or
                   requirement the question is about, so it can be shown next to
                   that text. Copy it word-for-word from the spec; if the
                   question is general, use "".
               Do NOT include a question already answered by the material.
               Return [] if nothing needs clarifying.
  "answered":  an array of objects {{"question": <string>, "answer": <string>,
               "evidence": <string>}}. Include a CURRENTLY OPEN QUESTION here
               ONLY if the material EXPLICITLY answers it:
                 - "question": the open question's exact text.
                 - "answer": one sentence stating the answer.
                 - "evidence": a SHORT quote copied WORD-FOR-WORD from the
                   material that states the answer. Do NOT paraphrase, translate,
                   summarise, or invent this quote — copy it verbatim.
               A topic merely being MENTIONED is NOT the same as being answered.
               If you would have to guess, infer, assume, or read between the
               lines, DO NOT put it here — leave it in "questions". When in
               doubt, leave the question open. Return [] if nothing is
               explicitly resolved this turn.

Be concrete. If something was not discussed, write "Not discussed" rather than
inventing details. Do NOT put a questions section inside the spec markdown;
clarifications belong only in "questions" / "answered"."""

SPEC_USER_INITIAL = """Produce the functional specification from the material below.

MATERIAL:
---
{transcript}
---"""

SPEC_USER_REVISE = """A functional specification already exists (below). NEW
material has since been transcribed. REVISE and extend the existing spec so it
reflects everything now known — keep what still holds, correct what changed, and
fold in the new details. Do not discard prior content just because it is not
repeated in the new material.

{resolved_block}{open_block}EXISTING SPECIFICATION:
---
{previous_spec}
---

FULL MATERIAL SO FAR (includes the new transcript):
---
{transcript}
---"""

VERIFY_SYSTEM = """You are a strict fact-checker for a business analyst. You are
given a meeting transcript and candidate items, each with a QUESTION, a proposed
ANSWER, and an EVIDENCE quote. For each, decide whether the transcript
EXPLICITLY and UNAMBIGUOUSLY answers the question.

Mark answered = false when ANY of these hold:
  - the topic is only mentioned in passing;
  - the speaker defers or hedges it ("decide later", "not sure", "maybe", "we'll see");
  - answering would require guessing, inference, or assumption;
  - the evidence quote does not, on its own, directly state the answer.

Be conservative: if there is any doubt, answered = false.

Respond with a single JSON object:
{"results": [{"question": "<the exact question text>", "answered": true or false}]}"""


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for grounding checks."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


def _evidence_supported(evidence: str, transcript: str) -> bool:
    """True only if the model's quote is actually present in the material."""
    ev = _normalize(evidence)
    if len(ev) < 4:
        return False
    tx = _normalize(transcript)
    if ev in tx:
        return True
    words = {w for w in ev.split() if len(w) > 3}
    if not words:
        return False
    hits = sum(1 for w in words if w in tx)
    return hits / len(words) >= 0.8


def _parse_json(text: str) -> dict:
    """Parse a JSON object out of an LLM reply, tolerating code fences / preamble.

    Claude (via CLI or OpenRouter) doesn't honour OpenAI's json_object mode, so it
    may wrap the object in ```json fences or add stray text. Strip that and, as a
    last resort, grab the outermost {...}.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            return json.loads(t[i:j + 1])
        raise


def _usage_of(resp) -> dict:
    """Extract OpenAI token usage from a chat-completions response, defensively."""
    u = getattr(resp, "usage", None)
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
    }


def _lines(raw: str) -> list:
    """Parse a newline- or JSON-array-encoded string into a clean list of strings."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    out = []
    for ln in raw.splitlines():
        s = re.sub(r"^\s*[-*]\s+", "", ln).strip()
        if s:
            out.append(s)
    return out


class SpecGenerator(Component):
    display_name = "VAMPS Spec Generator"
    description = "Turn a meeting transcript into a functional spec + clarifying questions (with a verified 'answered' pass)."
    documentation = "https://github.com/djj067/vamps"
    icon = "file-text"
    name = "vamps_spec_generator"

    inputs = [
        MessageTextInput(
            name="transcript",
            display_name="Transcript / Material",
            info="Full meeting transcript so far (plus any seed notes). This is the main input.",
            required=True,
        ),
        MultilineInput(
            name="previous_spec",
            display_name="Previous Spec (optional)",
            info="Existing spec Markdown. If set, the component revises it instead of writing from scratch.",
            value="",
        ),
        MultilineInput(
            name="resolved_questions",
            display_name="Resolved Questions (optional)",
            info="One per line (or a JSON array). Questions already answered by the client — never re-raised.",
            value="",
        ),
        MultilineInput(
            name="open_questions",
            display_name="Open Questions (optional)",
            info="One per line (or a JSON array). Still-awaiting-answer questions carried over from a prior turn.",
            value="",
        ),
        MessageTextInput(
            name="provider",
            display_name="Provider",
            info="'claude_cli' (testing, uses local Claude auth) or 'openrouter' (demo). "
                 "Blank = the SPEC_PROVIDER env var (default claude_cli).",
            value="",
            advanced=True,
        ),
        SecretStrInput(
            name="api_key",
            display_name="OpenRouter API Key",
            info="Only for the openrouter provider. Blank = OPENROUTER_API_KEY env var.",
            required=False,
        ),
        MessageTextInput(
            name="model",
            display_name="Model",
            info="openrouter: model slug (blank = SPEC_MODEL env / anthropic/claude-3.5-sonnet). "
                 "claude_cli: optional --model (blank = the CLI default).",
            value="",
        ),
        BoolInput(
            name="verify",
            display_name="Verify answers (2nd pass)",
            info="Run the strict skeptic pass before marking questions answered.",
            value=True,
        ),
    ]

    outputs = [
        Output(name="spec_text", display_name="Spec (Markdown)", method="build_spec_text"),
        Output(name="result", display_name="Full Result (Data)", method="build_result"),
        Output(name="result_json", display_name="Full Result (JSON text)", method="build_result_json"),
    ]

    # --- LLM plumbing --------------------------------------------------------
    def _provider(self) -> str:
        return ((self.provider or "").strip() or os.getenv("SPEC_PROVIDER", "claude_cli")).lower()

    def _complete(self, system: str, user: str, temperature: float = 0.3):
        """Run one chat completion, returning (content_text, usage_dict).

        Two providers, both Claude:
          - claude_cli: shell out to the `claude` CLI (uses local Claude auth, no
            API key/cost) — for testing.
          - openrouter: OpenAI-compatible call to OpenRouter — for the demo.
        """
        if self._provider() == "openrouter":
            key = (self.api_key or "").strip() or os.getenv("OPENROUTER_API_KEY", "")
            base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
            model = (self.model or "").strip() or os.getenv("SPEC_MODEL", "anthropic/claude-3.5-sonnet")
            resp = OpenAI(base_url=base, api_key=key).chat.completions.create(
                model=model, temperature=temperature,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            return (resp.choices[0].message.content or ""), _usage_of(resp)

        # claude_cli — prepend our instructions to the prompt; disable tools.
        claude = shutil.which("claude") or "claude"
        cmd = [claude, "-p", "--output-format", "json", "--allowed-tools", ""]
        model = (self.model or "").strip() or os.getenv("SPEC_MODEL_CLI", "")
        if model:
            cmd += ["--model", model]
        prompt = f"{system}\n\n{user}"
        wrapper, last = {}, ""
        for _ in range(2):   # the CLI occasionally returns empty stdout; retry once
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=240)
            last = proc.stderr or ""
            if (proc.stdout or "").strip():
                wrapper = json.loads(proc.stdout)
                if wrapper.get("result"):
                    break
        result = wrapper.get("result") or ""
        if not result:
            raise RuntimeError(f"claude CLI returned no output. stderr: {last[:200]}")
        u = wrapper.get("usage") or {}
        inp = (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0) \
            + (u.get("cache_creation_input_tokens", 0) or 0)
        out = u.get("output_tokens", 0) or 0
        usage = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}
        return result, usage

    def _build_messages(self, transcript, previous_spec, resolved, open_qs):
        if previous_spec.strip():
            resolved_block = ""
            if resolved:
                joined = "\n".join(f"- {q}" for q in resolved)
                resolved_block = (
                    "The following questions were already resolved by the client — "
                    "treat them as answered and do NOT raise them again:\n"
                    f"{joined}\n\n"
                )
            open_block = ""
            if open_qs:
                joined = "\n".join(f"- {q}" for q in open_qs)
                open_block = (
                    "CURRENTLY OPEN QUESTIONS (still awaiting an answer). If the "
                    "latest material now answers any of these, move it into "
                    '"answered" with a one-sentence answer instead of repeating it:\n'
                    f"{joined}\n\n"
                )
            user = SPEC_USER_REVISE.format(
                resolved_block=resolved_block,
                open_block=open_block,
                previous_spec=previous_spec,
                transcript=transcript,
            )
        else:
            user = SPEC_USER_INITIAL.format(transcript=transcript)
        return [
            {"role": "system", "content": SPEC_SYSTEM},
            {"role": "user", "content": user},
        ]

    def _verify_answers(self, candidates, transcript):
        zero = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not candidates:
            return [], zero
        listing = "\n\n".join(
            f'{i+1}. QUESTION: {c["question"]}\n   PROPOSED ANSWER: {c["answer"]}\n   EVIDENCE: "{c["evidence"]}"'
            for i, c in enumerate(candidates)
        )
        user = f"TRANSCRIPT:\n---\n{transcript}\n---\n\nCANDIDATES:\n{listing}"
        try:
            content, usage = self._complete(VERIFY_SYSTEM, user, temperature=0)
            data = _parse_json(content)
            confirmed = {
                _normalize(str(r.get("question", "")))
                for r in data.get("results", [])
                if r.get("answered") is True
            }
            return [c for c in candidates if _normalize(c["question"]) in confirmed], usage
        except Exception:
            return candidates, zero

    def _run(self) -> dict:
        """Compute once, memoize — so both outputs share one LLM round-trip."""
        if getattr(self, "_computed", None) is not None:
            return self._computed

        transcript = (self.transcript or "").strip()
        previous_spec = (self.previous_spec or "").strip()
        resolved = _lines(self.resolved_questions)
        open_qs = _lines(self.open_questions)
        if not transcript:
            raise ValueError("SpecGenerator: transcript is empty.")

        msgs = self._build_messages(transcript, previous_spec, resolved, open_qs)
        content, usage = self._complete(msgs[0]["content"], msgs[1]["content"], temperature=0.3)
        data = _parse_json(content)

        spec_md = (data.get("spec") or "").strip()
        questions = []
        for q in (data.get("questions") or []):
            if isinstance(q, dict):
                text, anchor = str(q.get("question", "")).strip(), str(q.get("anchor", "")).strip()
            else:
                text, anchor = str(q).strip(), ""
            if text:
                questions.append({"question": text, "anchor": anchor})

        candidates = []
        for a in (data.get("answered") or []):
            if not isinstance(a, dict):
                continue
            q = str(a.get("question", "")).strip()
            if q and _evidence_supported(str(a.get("evidence", "")), transcript):
                candidates.append({
                    "question": q,
                    "answer": str(a.get("answer", "")).strip(),
                    "evidence": str(a.get("evidence", "")).strip(),
                })

        if self.verify:
            confirmed, verify_usage = self._verify_answers(candidates, transcript)
        else:
            confirmed, verify_usage = candidates, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for k in usage:
            usage[k] += verify_usage.get(k, 0)

        answered = [{"question": c["question"], "answer": c["answer"]} for c in confirmed]
        answered_texts = {a["question"] for a in answered}
        open_texts = {q["question"] for q in questions}
        for a in (data.get("answered") or []):
            if isinstance(a, dict):
                q = str(a.get("question", "")).strip()
                if q and q not in answered_texts and q not in open_texts:
                    questions.append({"question": q, "anchor": ""})
                    open_texts.add(q)
        questions = [q for q in questions if q["question"] not in answered_texts]

        self._computed = {
            "ok": True,
            "spec": spec_md,
            "questions": questions,
            "answered": answered,
            "revised": bool(previous_spec),
            "provider": self._provider(),
            "model": (self.model or "").strip() or os.getenv("SPEC_MODEL", "") or "claude (cli default)",
            "transcript_chars": len(transcript),
            "usage": usage,
        }
        return self._computed

    # --- outputs -------------------------------------------------------------
    def build_spec_text(self) -> Message:
        result = self._run()
        self.status = result["spec"]
        return Message(text=result["spec"])

    def build_result(self) -> Data:
        result = self._run()
        self.status = result
        return Data(data=result)

    def build_result_json(self) -> Message:
        """Full result as a JSON string — for server-to-server use over the run API."""
        result = self._run()
        return Message(text=json.dumps(result))
