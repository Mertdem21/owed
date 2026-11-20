#!/bin/bash
# Owed: clear the Cognee memory and load the demo history (run once before recording the demo).
cd "$(dirname "$0")"
mkdir -p logs
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
{
echo "=== Owed memory reset $(date) ==="
uv run --no-sync python -m owed.cli memory-reset
echo "=== done $(date) ==="
} 2>&1 | tee logs/memory.log
