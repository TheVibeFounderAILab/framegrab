# Set up FrameGrab

## With a coding agent

Open this folder in Codex or Claude Code and paste START-HERE.txt.
The agent should check prerequisites and explain what will be installed.

## Manual setup (macOS / Linux)

Install Python 3.10+ and FFmpeg using your usual trusted package manager.
For example, on a Mac with Homebrew: `brew install python ffmpeg`.
On Debian/Ubuntu: `sudo apt install python3 python3-venv ffmpeg`.

In a terminal, change into this unzipped folder and run:

```sh
bash framegrab.sh
```

The launcher creates a local `venv/`, installs the pinned packages from
requirements.txt and starts the app. Open http://127.0.0.1:8900.
Keep the terminal open. Press Control-C to stop the app; saved files remain.
Relaunch with the same command. Installation needs internet access.

If port 8900 is already in use, run `PORT=8901 bash framegrab.sh` and open
http://127.0.0.1:8901. Do not stop another app to free the port.

## Optional transcription (Apple Silicon Mac only)

```sh
bash setup-transcription.sh
```

This creates `.transcribe-venv/` and installs Parakeet MLX. Restart FrameGrab
when it finishes. Model weights download on first use, which requires extra
disk space and time. No cloud transcription API key is needed. Transcription
installation and model inference were not retested for this workshop release.
Media download and frame capture work without it.

## First result

Paste one supported URL to media you own or have permission to use. Choose
video, audio or auto, then start the download. Wait for completion and review
the preview. Open the saved receipt to inspect the media and capture frames.
If transcription is enabled and succeeds, the receipt also contains a clean
script and timestamped transcript. Failed downloads show an error; support
varies by website and account access.

## Privacy

The launcher uses this folder's Downloads/ directory. No account cookies or
API keys are included. Browser-cookie access is off by default. Do not enable
it unless you understand and approve access to your own browser session.
Never paste cookies or credentials into a chat. Avoid confidential media if
your coding agent is connected to a cloud service unless you approve that use.

## Troubleshooting

- Missing Python / FFmpeg: install the named prerequisite, then try again.
- Installation fails: save the error and ask your coding agent to diagnose it.
- A source blocks downloading: try another permitted source; don't bypass
  access restrictions.
- No transcript: confirm you are on Apple Silicon, run optional setup and
  restart; inspect the transcription status before assuming it succeeded.
- To remove the app: stop it and remove its folder after saving wanted media.
