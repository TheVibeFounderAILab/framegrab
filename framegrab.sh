#!/bin/bash
# FrameGrab launcher · Copyright (c) 2026 TheVibeFounder
#
# Boots the standalone intake service on 127.0.0.1:${PORT:-8900}. Downloads
# default to this repository's ignored Downloads/ directory. Override with
# FRAMEGRAB_DOWNLOAD_DIR; select another engine with FRAMEGRAB_TRANSCRIBE_BIN.
set -euo pipefail
cd "$(dirname "$0")"

missing=()
command -v python3 >/dev/null 2>&1 || missing+=(python3)
command -v ffmpeg  >/dev/null 2>&1 || missing+=(ffmpeg)
if [ ${#missing[@]} -gt 0 ]; then
    echo "FrameGrab needs: ${missing[*]}"
    if command -v brew >/dev/null 2>&1; then
        echo "Install with:  brew install ${missing[*]}"
    elif command -v apt >/dev/null 2>&1; then
        echo "Install with:  sudo apt install ${missing[*]}"
    fi
    exit 1
fi

[ -d venv ] || { echo "Creating virtual environment..."; python3 -m venv venv; }
source venv/bin/activate
pip install -q -r requirements.txt

export PORT="${PORT:-8900}"
FRAMEGRAB_PROJECT_ROOT="$(pwd)"
# Workshop edition always uses its own library and loopback listener.
export HOST="127.0.0.1"
export FRAMEGRAB_DOWNLOAD_DIR="$FRAMEGRAB_PROJECT_ROOT/Downloads"
mkdir -p "$FRAMEGRAB_DOWNLOAD_DIR"

echo ""
echo "  FrameGrab is running at http://localhost:$PORT"
echo "  Receipts file into $FRAMEGRAB_DOWNLOAD_DIR"
echo ""
exec python3 app.py
