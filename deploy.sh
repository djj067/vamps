#!/usr/bin/env bash
# Deploy VAMPS to Render.
#
# ── One-time setup (Render dashboard, https://dashboard.render.com) ───────────
#   1. New ▸ Blueprint ▸ connect this GitHub repo (djj067/vamps).
#      Render reads render.yaml and creates the "vamps" Docker web service.
#      (Or: New ▸ Web Service ▸ Docker, repo=djj067/vamps, branch=master.)
#   2. On the service, set the three SECRET env vars:
#        DEEPGRAM_API_KEY          – Deepgram transcription
#        OPENROUTER_API_KEY        – spec generation (OpenRouter)
#        CLAUDE_CODE_OAUTH_TOKEN   – prototype build (Claude Code CLI)
#   3. (optional) Settings ▸ Deploy Hook ▸ copy the URL and export it:
#        export RENDER_DEPLOY_HOOK="https://api.render.com/deploy/srv-...?key=..."
#
# ── Every deploy after that ───────────────────────────────────────────────────
#   ./deploy.sh            # commits nothing; pushes what you've committed
#   ./deploy.sh -m "msg"   # stage all + commit with msg, then push
#
# Render auto-builds on push when the repo is connected; RENDER_DEPLOY_HOOK is
# only needed for manual/CI triggers.
set -euo pipefail
cd "$(dirname "$0")"

# Optional: -m "message" stages everything and commits before pushing.
if [ "${1:-}" = "-m" ] && [ -n "${2:-}" ]; then
  git add -A
  git commit -m "$2" || echo "  (nothing to commit)"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "▶ pushing $BRANCH → origin ..."
git push origin "$BRANCH"

if [ -n "${RENDER_DEPLOY_HOOK:-}" ]; then
  echo "▶ triggering Render deploy hook ..."
  curl -fsSL -X POST "$RENDER_DEPLOY_HOOK" >/dev/null && echo "  ✅ deploy triggered"
else
  echo "ℹ Render auto-deploys the connected branch on push."
  echo "  (For manual/CI deploys, set RENDER_DEPLOY_HOOK — see the header of this script.)"
fi
echo "✅ done — watch the build at https://dashboard.render.com"
