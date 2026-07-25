# VAMPS → Langflow 1.7.3

The VAMPS AI logic as Langflow custom components + flows. Spec generation runs
on **Claude via OpenRouter** (model `anthropic/claude-haiku-4.5`); the prototype
build shells out to the **Claude Code CLI**.

## What's here

```
vamps/spec_generator.py     Spec Generator component  (transcript -> functional spec + questions)
vamps/build_prototype.py    Build Prototype component (spec -> single-file HTML via Claude Code CLI)
vamps_spec_api_flow.json    Chat Input -> Spec Generator -> Text Output(JSON)   ← webpage calls this
vamps_build_api_flow.json   Chat Input -> Build Prototype -> Text Output(JSON)  ← webpage calls this
vamps_full_openrouter.json  Chat Input -> Spec -> Build -> Chat Output + Text Output  ← Playground demo
generate_flows.py           regenerates the three flow JSONs above
```

Two ways it's used:
- **Webpage** (`main.py`, port 8000, `LANGFLOW_ENABLED=true`): `/spec` and `/build`
  POST to Langflow's **VAMPS Spec API** / **VAMPS Build API** flows and read a JSON
  string off the Text Output. `main.py` resolves them by name and re-imports the
  JSON if a flow is missing or its edges were pruned.
- **Playground demo**: open **VAMPS Full (OpenRouter)** and run it — watch the
  Chat Input → Spec Generator → Build Prototype → outputs execute on the canvas.

## Run it

Langflow 1.7.3 needs Python 3.10–3.13.

```bash
uv pip install "langflow==1.7.3" "openai>=1.30" "httpx>=0.27" "python-docx>=1.1"
```

**Langflow** (spec runs here, so it needs the OpenRouter key + Claude on PATH):
```bash
OPENROUTER_API_KEY="$(grep '^OPENROUTER_API_KEY=' ../.env | cut -d= -f2-)" \
DEEPGRAM_API_KEY="$(grep '^DEEPGRAM_API_KEY=' ../.env | cut -d= -f2-)" \
LANGFLOW_COMPONENTS_PATH="$(pwd)" \
LANGFLOW_SKIP_AUTH_AUTO_LOGIN=true LANGFLOW_AUTO_LOGIN=true \
LANGFLOW_LOG_LEVEL=debug \
PATH="$HOME/.local/bin:$PATH" \
langflow run --host 127.0.0.1 --port 7860
```
Debug logging makes each webpage-triggered run visible:
`Running layer 1 with 1 tasks, ['Spec-vsa']` → `Vertex Out-vsa …`.

**The webpage**: `python -m uvicorn main:app --host 127.0.0.1 --port 8000` (from
the repo root), then open http://localhost:8000.

## Notes / gotchas

- **OpenRouter key can't be a tweak.** Langflow's run API rejects a request whose
  tweaks contain a secret-named field (`api_key`) and returns HTML, so the key
  must come from Langflow's own `OPENROUTER_API_KEY` env var. `main.py` only
  tweaks `provider` + `model`.
- **Edges must use compact handle strings.** Langflow's canvas computes each
  handle id with no spaces (`{œdataTypeœ:œ…œ,…}`); spaced strings make ReactFlow
  silently drop the edge, so `generate_flows.py` emits compact handles.
- **Flow loader constraints.** When a flow is imported from JSON, Langflow exec's
  the embedded component code and keeps only top-level imports and
  `def`/`class`/plain assignments — so components use flat imports and file-based
  session state (no annotated module globals).
- **Build** always uses the Claude Code CLI (`claude` logged in); only **spec**
  runs on OpenRouter.
