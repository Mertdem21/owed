#!/bin/bash
# Owed: replace the plain-text demo emails in your Gmail with the store-style HTML versions (12 demo emails only).
cd "$(dirname "$0")"
mkdir -p logs
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
{
echo "=== Owed Gmail refresh $(date) ==="
uv run --no-sync python -m scripts.remove_old_demo_emails
uv run --no-sync python -m scripts.send_test_emails --inbox demo
sleep 5
uv run --no-sync python -m owed.cli smoke gmail_imap
echo "=== done $(date) ==="
} 2>&1 | tee logs/gmail.log
