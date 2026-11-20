#!/bin/bash
# Owed: start (or restart) the web app on http://localhost:8000. Output also in logs/server.log
cd "$(dirname "$0")"
mkdir -p logs
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
OLD=$(lsof -ti tcp:8000 2>/dev/null)
if [ -n "$OLD" ]; then echo ">> stopping old server ($OLD)"; kill $OLD 2>/dev/null; sleep 1; fi
(sleep 4; open http://localhost:8000) &
uv run --no-sync uvicorn owed.app:app --port 8000 --reload --reload-dir owed 2>&1 | tee logs/server.log
