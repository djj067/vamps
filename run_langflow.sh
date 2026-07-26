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

echo "  refreshing the 3 VAMPS flows from disk ..."
$VENV/python - <<'PY' || true
import httpx, json
BASE="http://localhost:7860/api/v1/flows/"
FILES=["vamps_spec_api_flow","vamps_build_api_flow","vamps_full_openrouter"]
names={json.load(open(f"langflow_components/{p}.json"))["name"] for p in FILES}
# Match our flows exactly OR with Langflow's " (1)" collision suffix.
def is_ours(n): return any(n == t or n.startswith(t + " (") for t in names)
with httpx.Client(timeout=60) as h:
    body=h.get(BASE, params={"get_all":"true","header_flows":"true"}).json()
    items=body if isinstance(body, list) else body.get("items", [])
    # DELETE then re-import so the running flows always match the on-disk JSON.
    # Langflow's DB persists in the venv (not .langflow_data), so a name that already
    # exists is usually a STALE copy — keeping it caused "Could not find a JSON result".
    removed=0
    for f in items:
        if is_ours(f["name"]):
            h.delete(f"{BASE}{f['id']}"); removed+=1
    if removed:
        print(f"    removed {removed} stale VAMPS flow(s)")
    for p in FILES:
        name=json.load(open(f"langflow_components/{p}.json"))["name"]
        h.post(BASE, content=open(f"langflow_components/{p}.json","rb").read(),
               headers={"Content-Type":"application/json"})
        print(f"    imported: {name}")
PY

echo
echo "✅ Langflow ready → http://localhost:7860  (only the 3 VAMPS flows)"
echo "   Watch this terminal — webpage runs show as: Running layer 1 with 1 tasks, ['Spec-vsa']"
echo "   Ctrl-C to stop."
wait $LF_PID
