# VAMPS

Turns an **in-person meeting** into a **functional specification** in real time, then
lets you spin up a clickable prototype from that spec with one click.

Records the mic in short segments, transcribes each segment as it finishes (nothing
waits for the whole meeting), and continuously **drafts and revises** a functional
spec from the running transcript — filtering out off-topic chatter as it goes.

## What it does

- **Live transcript → spec.** Each clip is transcribed (Deepgram `nova-3`) and the
  spec is auto-revised (OpenAI `gpt-5.4-mini`), building on the previous version.
- **Off-topic filtering.** Small talk, logistics, and tangents are ignored — only
  product-relevant content shapes the spec.
- **Editable everywhere.** Transcript sections are foldable and editable; the spec
  itself is editable. Saved edits re-run the spec.
- **Recency-colored changes.** Each revision highlights what changed, color-coded by
  how recent (latest / previous / earlier) with a legend.
- **Clarifying questions.** The model surfaces what it still needs answered. It
  auto-resolves a question only when the conversation *explicitly* answers it —
  gated by a verbatim-evidence check plus a strict verification pass.
- **Prototype from spec.** "Build prototype" hands the spec to **Claude Code**
  (headless, file-tools only, sandboxed to a throwaway folder) to generate a
  self-contained `index.html` prototype, previewed in-app.
- **Export.** Download the spec as a Word `.docx`.

## Pipeline

```
Mic → ~60s segments → /transcribe (Deepgram nova-3) → live transcript (editable)
                                                        → /spec (gpt-5.4-mini) → functional spec + questions
                                        spec → /build (Claude Code) → index.html prototype
```

Every segment is also saved to `recordings/` as a local backup.

## Run

```bash
uv sync
uv run uvicorn main:app --reload --port 8000
# open http://127.0.0.1:8000  (use localhost/127.0.0.1 so the mic works)
```

## Configuration

Put your keys in a `.env` file (git-ignored):

```
OPENAI_API_KEY=...
DEEPGRAM_API_KEY=...
SPEC_MODEL=gpt-5.4-mini        # optional override
TRANSCRIBE_MODEL=nova-3        # optional override
TRANSCRIBE_LANGUAGE=en         # optional override
```

The prototype builder needs the [Claude Code](https://claude.com/claude-code) CLI
(`claude`) installed and authenticated on the machine running the server.

## Files

- `main.py` — FastAPI backend: `/transcribe`, `/spec`, `/upload_notes`, `/export`,
  `/build` (+ `/build/status`).
- `static/index.html` — the whole UI (recorder, tabbed transcript/spec/questions,
  build modal).

## Notes

- The transcript and spec live in the browser session — reloading clears them.
- `.env`, `recordings/`, and `generated/` are git-ignored.
