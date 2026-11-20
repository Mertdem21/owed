#!/bin/bash
# Owed: put the 12 demo emails into your Gmail inbox (nothing is sent), then test reading them. Output in logs/gmail.log
cd "$(dirname "$0")"
mkdir -p logs
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
{
echo "=== Owed Gmail seed $(date) ==="
uv run --no-sync python -m scripts.seed_gmail
sleep 5
uv run --no-sync python -m owed.cli smoke gmail_imap
echo "=== done $(date) ==="
} 2>&1 | tee logs/gmail.log
