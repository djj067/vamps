# VAMPS — FastAPI backend + Claude Code CLI (for /build prototype generation)
FROM python:3.12-slim

# Node.js (the Claude Code CLI is an npm package) + curl for the installer
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# uv for dependency management
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python deps first (better layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App source
COPY . .

# Render (and most hosts) inject $PORT; bind 0.0.0.0 so it's reachable.
ENV PORT=8000
CMD ["sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
