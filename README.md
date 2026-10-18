# FrameGrab

By [TheVibeFounder](https://github.com/thevibefounder).

[Download the latest ZIP](https://github.com/thevibefounder/framegrab/releases/latest/download/framegrab.zip)
· [Release notes](https://github.com/thevibefounder/framegrab/releases)
· [Chicago workshop](https://thevibefounder.com/chicago/ai-forge/)


By TheVibeFounder. Copyright (c) 2026 TheVibeFounder.

A local media workspace: paste a supported media link, save video, audio or
images, inspect your saved library, capture frames and create smaller preview
copies. Optional local transcription adds a script and timestamps.

## Start here

1. Unzip this folder on your computer.
2. Open the folder in Codex, Claude Code or another coding agent that can read
   files and run local commands.
3. Paste START-HERE.txt. The agent checks your setup and helps you launch.
4. Open http://127.0.0.1:8900 in your browser. Paste a link to media you own or
   have permission to download. Start with one short clip.
5. Review the result and open Downloads/ for the saved files.

A browser-only chat cannot run this app on your computer. See SETUP.md for
manual instructions. No API key or paid AI account is required by FrameGrab.
Your coding agent may have its own subscription or usage charges.

## Requirements and limits

- Python 3.10 or newer, FFmpeg (including ffprobe), and an internet connection
  for setup and remote media downloads. Python 3.12 is recommended.
- The included launcher targets macOS and Linux. This release was tested on
  macOS; Linux is supported by the script but was not independently tested.
  Windows/WSL has not been verified for this workshop edition.
- Transcription is optional and requires an Apple Silicon Mac. Run
  `bash setup-transcription.sh` to install Parakeet MLX separately. The first
  transcription can download model weights. This package does not bundle them.
- Website support depends on the source and downloader. Login requirements,
  restrictions, removed posts and site changes can prevent a download.
- Run on your own computer. This edition binds to 127.0.0.1 and is not a hosted
  multi-user service. Do not expose it through a public tunnel or reverse proxy.

## Your files

Downloads, job state and transcripts are stored in this folder's Downloads/
directory by default. This ZIP contains no saved media, credentials, cookies,
account settings, model weights or personal transcripts. The app contacts the
media sites you request; dependency installers contact their package hosts.
It does not send your library to TheVibeFounder. Sharing this folder after using
it can expose your own downloads. Keep Downloads/ and environment folders out
of any copy you share.

If transcription is unavailable, you can still save media and capture frames.
Do not treat a media-only result as a transcript. For an editing workflow, copy
the owned media into your separate Talking Head Video Agent input/ folder.

## License and attribution

Workshop edition v1.0.1. Original FrameGrab contributions are MIT-licensed;
see LICENSE. Preserve the creator credit and notices described in NOTICE.md.
The original reclip MIT notice and third-party licenses are in
ACKNOWLEDGMENTS.md and static/fonts/OFL-*.txt. Dependencies are installed
separately and keep their own licenses.

Verify attribution and source privacy with `python3 scripts/verify_credit.py`.

## Support and updates

If this helps your work, star the repository and
[follow TheVibeFounder](https://github.com/thevibefounder) for more builds.
Stars and follows are optional. Report reproducible problems through GitHub
Issues without sharing private files. See CONTRIBUTING.md before submitting a fix.
