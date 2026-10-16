# Acknowledgments

FrameGrab's lineage, stated plainly:

- **Origin.** FrameGrab began in April 2026 as a local fork of
  [averygan/reclip](https://github.com/averygan/reclip), an MIT-licensed
  web downloader (~194 lines of Flask + one HTML page). Nothing was ever
  pushed back to that project, and this tool is not affiliated with it.
- **What happened since.** FrameGrab is an MIT-derived, substantially
  rewritten project. It was rebuilt By TheVibeFounder as a local media-intake
  service: security/input hardening, bounded concurrency and per-domain
  throttling, media limits, H.264 normalization, Parakeet MLX transcription,
  the receipt contract (`<title>.mp4` + `.script.txt` + `.timestamps.txt`),
  the full test suite, the TVF v11.4.1 interface, and a restructuring of the
  remaining request/JS flow (done with the upstream source available, so it
  is a rewrite, not a clean-room process). The original contributions are
  authored By TheVibeFounder; copyright in those contributions does not extend
  to the MIT-licensed material they derive from.
- **What persists from reclip:** the shape of the product (paste URLs →
  yt-dlp fetch → poll a job id → save) and a residue of ordinary
  Flask/HTML/JS boilerplate lines. Both are covered by the MIT notice
  below and gratefully acknowledged.

## Original reclip license (MIT)

Retained as the MIT license requires for surviving portions of the
original work, and in good faith for the project FrameGrab descends from:

```
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Third-party components

| Component | License | Role |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | download engine (subprocess) |
| [gallery-dl](https://github.com/mikf/gallery-dl) | GPL-2.0 | image/gallery download engine (subprocess) |
| [FFmpeg](https://ffmpeg.org/) | LGPL/GPL | probe, convert, audio extract (subprocess) |
| [Flask](https://flask.palletsprojects.com/) | BSD-3 | web framework |
| [parakeet-mlx](https://github.com/senstella/parakeet-mlx) | Apache-2.0 | transcription (separate venv) |
| DM Serif Display, Inter, Geist Mono | OFL 1.1 | packaged fonts (`static/fonts/OFL-*.txt`) |

### Distribution note (FFmpeg / x264 / H.264)

FrameGrab invokes FFmpeg with `libx264` for H.264 normalization, and some FFmpeg builds include GPL components. This workshop ZIP
contains source code and licensed fonts only. It does not include FFmpeg,
x264, gallery-dl, yt-dlp, Flask, Parakeet, model weights, or a Docker image.
Install these dependencies separately; they retain their own licenses. Per [FFmpeg's legal
guidance](https://ffmpeg.org/legal.html), GPL components such as x264 can
make the distributed FFmpeg binary GPL-covered, and commercial H.264 use
may carry patent-licensing considerations. Running FFmpeg as a subprocess
does not make FrameGrab's own source GPL, but **any public or commercial
distribution of FrameGrab (especially the Docker image) requires a
packaging/license review first.** This source-only workshop edition is released under MIT for original
FrameGrab contributions. The original reclip MIT notice above is preserved.
