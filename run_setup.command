#!/bin/bash
# Owed: install everything, upload policies to Senso, test each integration, run the agent once.
# Double-click in Finder. Output also goes to logs/setup.log
cd "$(dirname "$0")"
mkdir -p logs
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
{
echo "=== Owed setup $(date) ==="
if ! command -v uv >/dev/null 2>&1; then
  echo ">> uv not found - installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo ">> $(uv --version)"
echo ">> uv sync --extra sponsors"
if uv sync --extra sponsors; then
  RUN="uv run --no-sync"
else
  echo "!! sponsor extra failed - installing core, then sponsor SDKs one by one"
  uv sync
  for p in singlestoredb amplitude-analytics amplitude-ai sie-sdk airbyte-agent-gmail; do
    uv pip install "$p" || echo "!! could not install $p (that integration will use its fallback)"
  done
  RUN="uv run --no-sync"
fi
echo ">> upload policies to Senso"
$RUN python -m scripts.seed_senso
echo ">> smoke tests"
$RUN python -m owed.cli smoke
echo ">> agent run"
$RUN python -m owed.cli run --reset
echo "=== done $(date) ==="
} 2>&1 | tee logs/setup.log
echo
echo "Fertig - die Ausgabe steht auch in logs/setup.log. Fenster kann offen bleiben."
