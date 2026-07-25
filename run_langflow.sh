#!/usr/bin/env bash
# Start Langflow for the VAMPS demo, in your own terminal so you see the logs.
#   - fresh, isolated DB (.langflow_data) with NO starter projects
#   - loads the VAMPS components, imports only the 3 VAMPS flows
#   - OpenRouter key + debug logging so webpage-triggered runs are visible
#
#   ./run_langflow.sh
# Then run the webpage in a second terminal:
#   PATH="$HOME/.local/bin:$PATH" .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
set -uo pipefail
cd "$(dirname "$0")"
VENV=.venv/bin

export PATH="$HOME/.local/bin:$PATH"                       # so `claude` is found
export LANGFLOW_CONFIG_DIR="$PWD/.langflow_data"           # isolated DB (gitignored)
export LANGFLOW_CREATE_STARTER_PROJECTS=false              # no example-flow clutter
export LANGFLOW_UPDATE_STARTER_PROJECTS=false
export LANGFLOW_SKIP_AUTH_AUTO_LOGIN=true                  # REQUIRED: no API key needed
export LANGFLOW_AUTO_LOGIN=true                            # (imports + webpage calls 403 without these)
export LANGFLOW_LOG_LEVEL=debug                            # show each run executing
export DO_NOT_TRACK=true
export LANGFLOW_COMPONENTS_PATH="$PWD/langflow_components"
export OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2-)"
export DEEPGRAM_API_KEY="$(grep -E '^DEEPGRAM_API_KEY=' .env | cut -d= -f2-)"

echo "▶ starting Langflow (fresh DB, no starter projects) on :7860 ..."
$VENV/langflow run --host 127.0.0.1 --port 7860 &
LF_PID=$!
trap 'echo; echo "stopping Langflow..."; kill $LF_PID 2>/dev/null || true' INT TERM EXIT

echo "  waiting for Langflow to come up ..."
until curl -sf http://localhost:7860/health_check >/dev/null 2>&1; do sleep 2; done

echo "  importing the 3 VAMPS flows (idempotent) ..."
$VENV/python - <<'PY' || true
import httpx, json
BASE="http://localhost:7860/api/v1/flows/"
FILES=["vamps_spec_api_flow","vamps_build_api_flow","vamps_full_openrouter"]
with httpx.Client(timeout=60) as h:
    body=h.get(BASE, params={"header_flows":"true"}).json()
    existing={f["name"] for f in (body if isinstance(body, list) else body.get("items", []))}
    for p in FILES:
        name=json.load(open(f"langflow_components/{p}.json"))["name"]
        if name in existing:
            print(f"    present: {name}")
        else:
            h.post(BASE, content=open(f"langflow_components/{p}.json","rb").read(),
                   headers={"Content-Type":"application/json"})
            print(f"    imported: {name}")
PY

echo
echo "✅ Langflow ready → http://localhost:7860  (only the 3 VAMPS flows)"
echo "   Watch this terminal — webpage runs show as: Running layer 1 with 1 tasks, ['Spec-vsa']"
echo "   Ctrl-C to stop."
wait $LF_PID
