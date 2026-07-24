# VAMPS → Langflow 1.7.3 port

The VAMPS backend logic, ported to **Langflow 1.7.3** custom components. Langflow
is a flow-execution engine, not a general web backend, so the port covers the
*logic*; a thin UI still handles mic capture and serving the generated
prototype (see [Caveats](#caveats)).

## The live-voice app (the hybrid)

Langflow can't be the app itself — it has no microphone and no custom web UI.
The shipping app stays the existing `static/index.html` + `main.py`, which does
live mic capture, and calls Langflow for the AI steps:

```
Browser (static/index.html)   ← live mic, records segments, shows spec/prototype
      │
      ▼
main.py (thin shell)
  /transcribe → Deepgram directly        ← stays direct: per-segment, low latency
  /spec       → Langflow  VAMPS Spec API   flow
  /build      → Langflow  VAMPS Build API  flow (background thread, keeps polling)
```

Enable it by setting `LANGFLOW_API_KEY` in `.env` (see that file). Blank = the
original inline behaviour. Setup:

1. Import `vamps_spec_api_flow.json` and `vamps_build_api_flow.json` into Langflow
   (names must match `LANGFLOW_SPEC_FLOW` / `LANGFLOW_BUILD_FLOW`).
2. Create a Langflow API key (Settings → Langflow API Keys) → put it in `.env`.
3. Make sure Langflow's own env has `OPENAI_API_KEY` + `DEEPGRAM_API_KEY` and
   `claude` on PATH (the components run there, not in `main.py`).
4. Run `main.py` as before; `/spec` and `/build` now execute in Langflow.

`main.py` resolves the flow ids by name via the Langflow API, POSTs to
`/api/v1/run/{id}`, and reads a JSON string off each flow's **Text Output** (the
`result_json` component output). The build flow blocks ~30–90s, so `/build` runs
it on a background thread and the browser keeps polling `/build/status` exactly
as before; the returned HTML is written under `generated/` and served locally.

## Components

| Component (`name`) | Ported from `main.py` | Output |
|---|---|---|
| `VAMPS Spec Generator` (`vamps_spec_generator`) | `/spec` — spec gen + evidence check + verify pass | `spec_text` (Message), `result` (Data: spec/questions/answered/usage) |
| `VAMPS Build Prototype` (`vamps_build_prototype`) | `/build` (blocking, with session resume) | `html` (Message), `result` (Data: path/tokens/session) |
| `VAMPS Deepgram Transcribe` (`vamps_deepgram_transcribe`) | `/transcribe` (per-file) | `text` (Message), `result` (Data) |
| `VAMPS Notes Parser` (`vamps_notes_parser`) | `/upload_notes` | `text` (Message), `result` (Data) |

`/export` (spec → .docx) was intentionally **not** ported — it is pure file
formatting with no LLM step; keep it in a thin shell if you need it.

## Install

Langflow 1.7.3 requires **Python 3.10–3.13** (not 3.14).

```bash
# fresh venv recommended
uv venv --python 3.12 && source .venv/bin/activate    # or python -m venv
uv pip install "langflow==1.7.3"

# runtime deps these components import (must share Langflow's env):
uv pip install "openai>=1.30" "httpx>=0.27" "python-docx>=1.1"
```

## Configure & run

```bash
# point Langflow at this components folder (the parent of vamps/)
export LANGFLOW_COMPONENTS_PATH="$(pwd)/langflow_components"

# credentials (or type keys into the component fields in the UI)
export OPENAI_API_KEY=sk-...
export DEEPGRAM_API_KEY=...
export CLAUDE_CODE_OAUTH_TOKEN=...     # for Build Prototype; `claude` must be on PATH

langflow run                 # UI + API at http://127.0.0.1:7860
# or, API only:
langflow run --backend-only
```

Components appear under a **vamps** category in the component sidebar. Langflow
discovers a subfolder-per-category, so the layout must stay
`langflow_components/vamps/*.py`.

## Ready-made flows (drag to import)

Both are pre-wired and **validated against 1.7.3's own graph loader** (built with
Langflow's `Graph` API — see `generate_flows.py`, re-run it to regenerate):

| File | Flow | Needs |
|---|---|---|
| **`vamps_full_flow.json`** | Deepgram + Notes → Combine → **Spec Generator** → **Build Prototype** → Chat Output (html) + Text Output (spec) | all of the below |
| `vamps_spec_flow.json` | Chat Input → **Spec Generator** → Chat Output | `OPENAI_API_KEY` |
| `vamps_build_flow.json` | Chat Input (spec) → **Build Prototype** → Chat Output (index.html) | `claude` on PATH + logged in |

`vamps_full_flow.json` is the whole VAMPS pipeline in one flow — all four custom
components plus a Combine Text node:

```
Deepgram Transcribe (audio) ─┐
                             ├─ Combine ─ Spec Generator ─┬─ Build Prototype ─ Chat Output (index.html)
Notes Parser (docx/txt) ─────┘                            └─ Text Output (spec markdown)
```

To run it: upload an **audio file** to the Deepgram node and a **notes file** to
the Notes node (both have file pickers on the node), set `OPENAI_API_KEY` +
`DEEPGRAM_API_KEY`, and Playground. If you only have audio, delete the
Notes + Combine nodes and wire Deepgram's `text` straight into Spec Generator's
`transcript`.

Import: Langflow home → **New Flow ▾ → Import**, pick the file (or drag the
`.json` onto the flows list). Open it and hit **Playground**.

The **build flow runs the HTML generation inside Langflow** — verified
end-to-end: the Build Prototype component shells out to the Claude Code CLI
(blocking), writes `<output_root>/<project_id>/index.html`, and returns the HTML
as the chat output. It persists the CLI session id to
`<output_root>/<project_id>/.vamps_session.json`, so a rebuild with the same
`project_id` resumes that session and edits the existing prototype in place.
Paste a spec into the Playground to try it.

## Wiring a flow manually

A minimal spec flow:

```
Chat Input ──(transcript)──▶ VAMPS Spec Generator ──(spec_text)──▶ Chat Output
```

Feed prior state back on later turns via the `previous_spec` / `open_questions`
/ `resolved_questions` inputs (set them per-run through `tweaks`, below).

Full pipeline:

```
Notes Parser ─┐
              ├─▶ (concatenate) ─▶ Spec Generator ─(spec_text)─▶ Build Prototype ─▶ Chat Output
Deepgram ─────┘
```

## Calling the flow from your UI

Get an API key from the Langflow UI (**Settings → API Keys**), then:

```bash
curl -X POST "http://127.0.0.1:7860/api/v1/run/<FLOW_ID>" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $LANGFLOW_API_KEY" \
  -d '{
    "input_value": "…full transcript…",
    "input_type": "chat",
    "output_type": "chat",
    "session_id": "meeting-123",
    "tweaks": {
      "vamps_spec_generator": {
        "previous_spec": "…prior spec markdown…",
        "open_questions": "What is the launch date?\nWhich auth provider?"
      }
    }
  }'
```

Notes:
- `input_value` is a **string**. To pass structured fields (previous spec, open
  questions) use `tweaks` keyed by the component `name`, as above.
- The `result` (Data) output carries the full structured object
  (`spec`, `questions`, `answered`, `usage`); wire that output to a Chat/Data
  output if your caller needs the JSON rather than just the Markdown.
- To upload a notes/audio file for a run: `POST /api/v2/files` (multipart), then
  pass the returned path into the File component via `tweaks`.

## Caveats

- **Blocking build.** `Build Prototype` runs the Claude Code CLI with
  `subprocess.run` and waits (30–90s typical). The `/api/v1/run` call blocks for
  that whole time — raise your client timeout. There is no progress/cancel
  (that was the async machinery we deliberately dropped).
- **Session resume** is kept, via a per-project file
  (`<output_root>/<project_id>/.vamps_session.json`). Pass a stable `project_id`
  across rebuilds to edit the existing `index.html` in place. (A module-level
  dict can't be used — see the editing note below.)
- **Serving the prototype is not Langflow's job.** `Build Prototype` returns the
  HTML as text and a path on disk; your UI renders/hosts it.
- **Real-time mic capture** (record → segment → upload) stays in the browser/UI.
  `Deepgram Transcribe` transcribes a whole uploaded file per run.

## Editing the components (loader gotchas)

When a flow is imported from JSON, Langflow **exec's the component's embedded
code**, and its scope-prep keeps only top-level **imports** and **`def` / `class`
/ plain `NAME = value` assignments**. Two things get silently dropped and cause
`NameError`s *at run time* (not at import/validate time):

1. **Imports inside `try/except`** — keep imports flat at module top level.
2. **Annotated module-level assignments** (`X: dict = {}`) — the annotation makes
   it an `AnnAssign`, which is not collected. Use a plain `X = {}`, or (for
   cross-run state) a file, since re-exec resets module globals anyway.

After editing a component, re-run `python generate_flows.py` so the JSON
re-embeds the new code, and restart `langflow run` to refresh the sidebar.
