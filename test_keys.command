#!/bin/bash
# Owed: quick check of every integration key (no install, no agent run). Output also in logs/smoke.log
cd "$(dirname "$0")"
mkdir -p logs
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
{
echo "=== Owed key test $(date) ==="
uv run --no-sync python -m owed.cli smoke
echo "=== done $(date) ==="
} 2>&1 | tee logs/smoke.log
echo
echo "Fertig - Ausgabe steht in logs/smoke.log. Fenster kann zu."
