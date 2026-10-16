#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "Parakeet MLX transcription requires an Apple Silicon Mac."
    echo "FrameGrab still works in media-only mode without this setup."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required. Install it, then rerun this script."
    exit 1
fi

if [ ! -d ".transcribe-venv" ]; then
    python3 -m venv .transcribe-venv
fi

.transcribe-venv/bin/python -m pip install --upgrade pip
.transcribe-venv/bin/python -m pip install -r requirements-transcribe.txt

if [ ! -x ".transcribe-venv/bin/parakeet-mlx" ]; then
    echo "Transcription setup did not produce .transcribe-venv/bin/parakeet-mlx"
    exit 1
fi

echo "Transcription is ready. Start FrameGrab with ./framegrab.sh"
