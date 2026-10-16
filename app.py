"""FrameGrab — standalone local URL-to-media/assets/script intake.

Copyright (c) 2026 TheVibeFounder. All rights reserved.

Local Flask service: paste a link and automatically extract normalized video,
numbered images, or mixed-media bundles plus optional script/timestamp receipts.
Provenance: see ACKNOWLEDGMENTS.md.
"""
import itertools
import math
import os
import signal
import random
import re
import time
import uuid
import glob
import json
import shutil
import sys
import threading
import subprocess
import urllib.parse
import zipfile
import functools
import hashlib
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from framegrab_core.storage import Store
from framegrab_core.artifacts import read_manifest
from framegrab_core.library import register as register_library
from framegrab_core.runtime import acquire_service_lock

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MB request bodies

@app.before_request
def workshop_local_access():
    """Keep this local app inaccessible to other sites and DNS rebinding."""
    try:
        target = urllib.parse.urlsplit(request.host_url)
        if target.hostname not in ("127.0.0.1", "localhost", "::1"):
            return jsonify(error="FrameGrab is a local-only app"), 403
        origin = request.headers.get("Origin")
        if origin:
            source = urllib.parse.urlsplit(origin)
            def authority(url):
                return (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80))
            if authority(source) != authority(target):
                return jsonify(error="Cross-origin requests are not allowed"), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="Cross-site requests are not allowed"), 403
    except ValueError:
        return jsonify(error="Invalid request origin"), 403



def _env(name, default=None):
    """Read FRAMEGRAB_<name>, honoring the pre-rename VIDCLIP_<name> as well.

    This project was VidClip before it was FrameGrab. A launch script, tmux
    session or shell profile still exporting the old variable has to keep
    working, because the failure mode of ignoring it is silent and looks awful:
    VIDCLIP_DOWNLOAD_DIR would fall through to the in-repo default and every
    receipt in the real library would appear to have vanished.
    """
    for prefix in ("FRAMEGRAB_", "VIDCLIP_"):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


def resolve_transcription_enabled(mode, binary):
    mode = mode.strip().lower()
    if mode not in ("auto", "0", "1"):
        raise RuntimeError("FRAMEGRAB_TRANSCRIBE must be 'auto', '0', or '1'")
    return mode == "1" or (
        mode == "auto" and os.path.isfile(binary) and os.access(binary, os.X_OK)
    )

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_DIR = os.path.abspath(
    os.path.expanduser(
        _env("DOWNLOAD_DIR")  # FRAMEGRAB_, then the VidClip-era name
        or os.environ.get("RECLIP_DOWNLOAD_DIR")  # and the reclip-era one before that
        or os.path.join(PROJECT_ROOT, "Downloads")
    )
)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
service_lock = acquire_service_lock(DOWNLOAD_DIR) if __name__ == '__main__' or _env('SERVICE_LOCK', '0') == '1' else None
BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

# The ledger makes receipts survive restarts: one JSON line per finished job,
# kept next to the files it describes so deleting downloads/ clears both.
LEDGER_PATH = os.path.join(DOWNLOAD_DIR, ".receipts.jsonl")

TRANSCRIBE_BIN = os.path.abspath(
    os.path.expanduser(
        _env("TRANSCRIBE_BIN")
        or os.path.join(PROJECT_ROOT, ".transcribe-venv", "bin", "parakeet-mlx")
    )
)
TRANSCRIBE_MODE = _env("TRANSCRIBE", "auto").strip().lower()
TRANSCRIBE_ENABLED = resolve_transcription_enabled(TRANSCRIBE_MODE, TRANSCRIBE_BIN)
TRANSCRIBE_TIMEOUT_S = int(_env("TRANSCRIBE_TIMEOUT_S", "1800"))
DOWNLOAD_TIMEOUT_S = int(_env("DOWNLOAD_TIMEOUT_S", "900"))
DOWNLOAD_STALL_S = float(_env("DOWNLOAD_STALL_S", "90"))
CONVERT_TIMEOUT_S = int(_env("CONVERT_TIMEOUT_S", "1800"))
# Compression is a two-pass encode of a file already on disk, so it gets its own
# (longer) budget: the download timeout has nothing to say about it.
COMPRESS_TIMEOUT_S = int(_env("COMPRESS_TIMEOUT_S", "3600"))
X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)
# Whitelisted rather than passed through: this value becomes an ffmpeg argument,
# and a typo would only surface as a failed encode minutes later.
COMPRESS_PRESET = _env("COMPRESS_PRESET", "medium").strip().lower()
if COMPRESS_PRESET not in X264_PRESETS:
    COMPRESS_PRESET = "medium"
COMPRESS_PRESETS_MB = (8, 25, 50, 100, 512)  # the caps offered in the UI
COMPRESS_MIN_TARGET_MB = 1
COMPRESS_MIN_VIDEO_KBPS = 80  # below this even 240p stops resembling the source
COMPRESS_AUDIO_LADDER = (128, 96, 64, 48, 32)
# Bits are not the only cost of a container: mp4 headers, per-frame boxes and
# x264's own rate error all land on top of the requested bitrate, and a cap
# missed by one percent is a cap missed.
COMPRESS_SAFETY = 0.97
# (video kbps ceiling, max height). 240 kbps of 1080p is mush; the same bits at
# 480p are watchable. Only ever downscale — see compress_scale_height.
COMPRESS_HEIGHT_LADDER = ((300, 360), (700, 480), (1500, 720), (3000, 1080))
MAX_PLAYLIST_ITEMS = int(_env("MAX_PLAYLIST_ITEMS", "50"))
MAX_CONCURRENT_JOBS = int(_env("MAX_CONCURRENT_JOBS", "2"))
MAX_ACTIVE_JOBS = int(_env("MAX_ACTIVE_JOBS", "12"))
DOMAIN_MIN_GAP_S = float(_env("DOMAIN_MIN_GAP_S", "8"))
DOMAIN_GAP_JITTER_S = float(_env("DOMAIN_GAP_JITTER_S", "4"))
MAX_DURATION_S = int(_env("MAX_DURATION_S", "7200"))
MAX_FILESIZE_MB = int(_env("MAX_FILESIZE_MB", "4096"))
MAX_IMAGES_PER_JOB = int(_env("MAX_IMAGES_PER_JOB", "50"))
MAX_IMAGE_FILESIZE_MB = int(_env("MAX_IMAGE_FILESIZE_MB", "100"))
MIN_FREE_DISK_MB = int(_env("MIN_FREE_DISK_MB", "2048"))
JOB_TTL_S = int(_env("JOB_TTL_S", str(24 * 3600)))

FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
TERMINAL_STATUSES = ("done", "error")

# SQLite owns durable job checkpoints. Memory holds the live worker view;
# a restart surfaces interrupted jobs for explicit recovery.
store = Store(os.path.join(DOWNLOAD_DIR, '.framegrab.sqlite3'))
jobs = store.recover()
jobs_lock = threading.RLock()
worker_context = threading.local()
active_workers = set()
manifest_lock = threading.RLock()
download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
transcribe_slot = threading.Lock()  # Parakeet/Metal: one transcription at a time
finalize_lock = threading.Lock()  # makes collision-suffix allocation atomic
ledger_lock = threading.Lock()  # serializes ledger appends and rewrites


def serialized_manifest(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with manifest_lock:
            return function(*args, **kwargs)
    return wrapped

# Compressions are keyed by receipt id, not job id, because a receipt outlives
# the job that made it: the ledger and the package folder survive a restart, so
# compressing a receipt from last week must work exactly like compressing the
# one that finished a second ago. State here is only the in-flight picture — the
# finished artifact is recorded in the ledger entry and the folder manifest.
compressions = {}
compressions_lock = threading.Lock()
compress_slot = threading.Lock()  # one x264 at a time: two saturate every core

# ── domain throttle ──────────────────────────────────────────────────────────
# Hammering one host with back-to-back downloader launches is how a source IP gets
# rate-limited or blocked (X/Twitter especially, including bulk submissions
# from the UI). Download STARTS for the same registrable host are strictly
# paced: at least DOMAIN_MIN_GAP_S apart plus 0..DOMAIN_GAP_JITTER_S of jitter
# so the cadence is not robotic. Downloads may still overlap once started —
# only the start burst is smoothed. Jobs keep status "queued" while pacing.
_DOMAIN_ALIASES = {
    "twitter.com": "x.com",
    "t.co": "x.com",
    "twimg.com": "x.com",
    "fxtwitter.com": "x.com",
}
_domain_state_lock = threading.Lock()
_domain_state = {}  # host -> {"lock": threading.Lock, "last_start": float}

_RATE_LIMIT_RE = re.compile(r"429|403|rate.?limit|too many requests|forbidden", re.I)


def domain_key(url):
    """Registrable-host bucket for pacing (twitter/twimg fold into x.com)."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) > 2:
        host = ".".join(parts[-2:])
    return _DOMAIN_ALIASES.get(host, host)


def throttle_domain(url):
    """Block until this host's next allowed start time; returns the host key."""
    host = domain_key(url)
    if not host:
        return host
    with _domain_state_lock:
        st = _domain_state.setdefault(host, {"lock": threading.Lock(), "last_start": 0.0})
    with st["lock"]:  # held through the sleep: same-host starts stay gap-apart
        gap = DOMAIN_MIN_GAP_S + random.uniform(0, DOMAIN_GAP_JITTER_S)
        wait = st["last_start"] + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        st["last_start"] = time.monotonic()
    return host


def classify_download_error(stderr):
    """Last stderr line, prefixed when the failure is rate-limit-shaped so
    clients know to cool down instead of instantly retrying."""
    line = stderr.strip().split("\n")[-1] if stderr.strip() else "download failed"
    if _RATE_LIMIT_RE.search(stderr):
        return "rate-limited by source — cool down before retrying: " + line
    return line


def parse_json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Request body must be a JSON object"}), 400)
    return data, None


def validate_url(url):
    if not isinstance(url, str):
        return None
    url = url.strip()
    if len(url) > 2048:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


_FILENAME_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|]')


def safe_download_name(title, ext, fallback):
    """A filesystem-safe receipt name: printable, no path/reserved chars,
    capped at 100 chars, never hidden — falls back to the job id."""
    if not isinstance(title, str):
        title = ""
    printable = "".join(ch for ch in title if ch.isprintable())
    stem = _FILENAME_FORBIDDEN_RE.sub("", printable).strip()[:100].strip().lstrip(".")
    return f"{stem or fallback}{ext}"


def job_package_dir(job_id, job=None):
    """Stable, self-contained folder for one extraction receipt."""
    job = job or jobs[job_id]
    if job.get("folder"):
        return job["folder"]
    stem = safe_download_name(job.get("title", ""), "", "Download")
    folder_name = f"{stem} [{job_id}]"
    folder = os.path.join(DOWNLOAD_DIR, folder_name)
    os.makedirs(os.path.join(folder, "Frames"), exist_ok=True)
    job["folder"] = folder
    job["folder_name"] = folder_name
    job["frames_dir"] = os.path.join(folder, "Frames")
    return folder


def unique_path(path):
    """First free path: exact name, then ' 2', ' 3', ... — never overwrite."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for n in itertools.count(2):
        candidate = f"{stem} {n}{ext}"
        if not os.path.exists(candidate):
            return candidate


def finalize_path(src, target):
    with finalize_lock:
        final = unique_path(target)
        if os.path.abspath(src) != os.path.abspath(final):
            os.rename(src, final)
    return final


def free_disk_mb():
    return shutil.disk_usage(DOWNLOAD_DIR).free // (1024 * 1024)


def prune_jobs(now=None):
    now = now if now is not None else time.time()
    with jobs_lock:
        expired = [
            job_id
            for job_id, job in jobs.items()
            if (job.get("status") == "done" or job.get("dismissed"))
            and now - job.get("finished_at", now) > JOB_TTL_S
        ]
        for job_id in expired:
            del jobs[job_id]


def active_job_count():
    with jobs_lock:
        return sum(1 for j in jobs.values() if j.get("status") not in TERMINAL_STATUSES)


def reserve_job(job_id, job):
    """Atomically enforce the active-job cap and reserve a queue slot."""
    with jobs_lock:
        active = sum(1 for j in jobs.values() if j.get("status") not in TERMINAL_STATUSES)
        if active >= MAX_ACTIVE_JOBS:
            return False
        job.setdefault('created_at', time.time())
        store.save_job(job_id, job)
        jobs[job_id] = job
        return True


class JobCancelled(RuntimeError):
    pass


def check_cancel():
    job_id = getattr(worker_context, 'job_id', None)
    if job_id and jobs.get(job_id, {}).get('cancel_requested'):
        raise JobCancelled('Cancelled. Saved source media has been kept.')


def checkpoint(job_id):
    store.save_job(job_id, jobs[job_id])


def cleanup_job_artifacts(job_id):
    """Remove only temporary files owned by a failed job."""
    paths = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
    paths += glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_h264.*"))
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(os.path.join(DOWNLOAD_DIR, ".image-work", job_id), ignore_errors=True)
    job = jobs.get(job_id, {})
    folder = job.get("folder")
    if folder and _path_within_downloads(folder):
        shutil.rmtree(folder, ignore_errors=True)


JOB_ID_RE = re.compile(r"^[0-9a-f]{10}$")


def _ledger_relative(path):
    """Portable artifact path relative to Downloads, or None if it escapes."""
    if not _path_within_downloads(path):
        return None
    return os.path.relpath(os.path.realpath(path), os.path.realpath(DOWNLOAD_DIR))


def _path_within_downloads(path):
    if not path:
        return False
    root = os.path.realpath(DOWNLOAD_DIR)
    candidate = os.path.realpath(path)
    try:
        return os.path.commonpath([root, candidate]) == root and candidate != root
    except ValueError:
        return False


def reveal_local_artifact(path):
    """Reveal one validated saved artifact in the platform file manager."""
    if not _path_within_downloads(path) or not os.path.isfile(path):
        raise FileNotFoundError("Saved artifact not found")
    if sys.platform == "darwin":
        command = ["open", "-R", path]
        manager = "Finder"
    elif os.name == "nt":
        command = ["explorer", f"/select,{os.path.normpath(path)}"]
        manager = "File Explorer"
    else:
        command = ["xdg-open", os.path.dirname(path)]
        manager = "file manager"
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return manager


def reveal_response(path):
    """Launch only same-origin UI reveal requests for known saved artifacts."""
    if request.headers.get("X-FrameGrab-Action") != "reveal":
        return jsonify({"error": "Reveal action header required"}), 403
    if not path or not _path_within_downloads(path) or not os.path.isfile(path):
        return jsonify({"error": "Saved artifact not found"}), 404
    try:
        manager = reveal_local_artifact(path)
    except OSError as exc:
        return jsonify({"error": f"Could not open file manager: {exc}"}), 500
    return jsonify({"ok": True, "manager": manager})


@serialized_manifest
def write_job_manifest(job_id, job):
    """Write the machine-readable handoff every local agent can rely on."""
    folder = job_package_dir(job_id, job)
    job.setdefault("saved_at", time.time())
    artifacts = {}
    for key in ("file", "preview", "script", "transcript", "compressed"):
        path = job.get(key)
        if path and os.path.exists(path):
            artifacts[key] = {
                "filename": os.path.basename(path),
                "relative_path": _ledger_relative(path),
                "absolute_path": os.path.abspath(path),
            }
    artifacts["sources"] = [
        {
            "filename": os.path.basename(path),
            "relative_path": _ledger_relative(path),
            "absolute_path": os.path.abspath(path),
        }
        for path in job.get("files", []) if os.path.exists(path)
    ]
    manifest = {
        "schema_version": 1,
        "id": job_id,
        "source_url": job.get("url"),
        "video_name": job.get("title", ""),
        "creator": job.get("uploader", ""),
        "saved_at": job["saved_at"],
        "folder": os.path.abspath(folder),
        "frames_folder": os.path.abspath(job["frames_dir"]),
        "artifacts": artifacts,
    }
    path = os.path.join(folder, "manifest.json")
    if os.path.isfile(path):
        existing = read_manifest(folder)
        manifest['artifacts'] = {**existing.get('artifacts', {}), **artifacts}
        manifest = {**existing, **manifest}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    job["manifest"] = path
    job["manifest_filename"] = "manifest.json"
    return path


def record_receipt(job_id, job):
    """Append one finished job using safe Downloads-relative artifact paths."""
    entry = {
        "id": job_id,
        "saved_at": job.get("saved_at", time.time()),
        "url": job.get("url"),
        "title": job.get("title", ""),
        "uploader": job.get("uploader", ""),
        "duration": job.get("duration"),
        "thumbnail": job.get("thumbnail", ""),
        "detected_format": job.get("detected_format"),
        "file_count": job.get("file_count"),
        "filename": job.get("filename"),
        "file": _ledger_relative(job.get("file")),
        "preview": _ledger_relative(job.get("preview")),
        "folder": _ledger_relative(job.get("folder")),
        "manifest": _ledger_relative(job.get("manifest")),
        "script": _ledger_relative(job.get("script")) if job.get("script_status") == "done" else None,
        "script_filename": job.get("script_filename"),
        "package_status": job.get("package_status", "done"),
        "transcript": _ledger_relative(job.get("transcript")) if job.get("script_status") == "done" else None,
        "transcript_filename": job.get("transcript_filename"),
    }
    with ledger_lock:
        existing = next((item for item in load_ledger() if item.get('id') == job_id), {})
        entry = {**existing, **entry}
        with open(LEDGER_PATH, "a") as ledger:
            ledger.write(json.dumps(entry) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())


def load_ledger():
    """Ledger entries newest-first, keeping only those whose receipt file still
    exists so hand-deleted downloads silently drop out of the history."""
    try:
        with open(LEDGER_PATH) as ledger:
            lines = ledger.readlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        name = entry.get("file")
        if ledger_artifact_path(name):
            entries.append(entry)
    # A list limit must never make an older receipt inaccessible by ID.
    return list({entry['id']: entry for entry in entries}.values())[::-1]


def remove_ledger_entry(entry_id):
    """Drop one entry from the ledger file; the files themselves are kept."""
    with ledger_lock:
        try:
            with open(LEDGER_PATH) as ledger:
                lines = ledger.readlines()
        except OSError:
            return False
        kept, removed = [], False
        for line in lines:
            try:
                if json.loads(line).get("id") == entry_id:
                    removed = True
                    continue
            except ValueError:
                continue
            kept.append(line)
        if removed:
            tmp = LEDGER_PATH + ".tmp"
            with open(tmp, "w") as ledger:
                ledger.writelines(kept)
            os.replace(tmp, LEDGER_PATH)
        return removed


def update_ledger_entry(entry_id, patch):
    """Merge `patch` into one entry, rewriting the ledger. Returns whether the
    entry was found. Append-only is the rule for *receipts*; a receipt gaining a
    later artifact is the one exception, and it is still a whole-file rewrite
    through a temp file so a crash mid-write cannot truncate the history."""
    with ledger_lock:
        try:
            with open(LEDGER_PATH) as ledger:
                lines = ledger.readlines()
        except OSError:
            return False
        kept, found = [], False
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a line we cannot parse is a line we must not rewrite
            if entry.get("id") == entry_id:
                entry.update(patch)
                found = True
                line = json.dumps(entry) + "\n"
            kept.append(line)
        if found:
            tmp = LEDGER_PATH + ".tmp"
            with open(tmp, "w") as ledger:
                ledger.writelines(kept)
            os.replace(tmp, LEDGER_PATH)
        return found


@serialized_manifest
def patch_manifest(folder, key, path):
    """Add one artifact to an existing folder manifest, leaving the rest alone.

    write_job_manifest owns the manifest at download time and has a live job to
    read; an artifact produced later (a compression, days after the fact) has
    only the folder, so it patches in place rather than rebuilding from nothing.
    """
    manifest_path = os.path.join(folder, "manifest.json")
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not an object")
    manifest.setdefault("artifacts", {})[key] = {
        "filename": os.path.basename(path),
        "relative_path": _ledger_relative(path),
        "absolute_path": os.path.abspath(path),
    }
    tmp = manifest_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, manifest_path)
    return manifest_path


@serialized_manifest
def patch_manifest_compressed_variants(folder, variants, latest_path):
    """Write the complete cap index while retaining a latest compatibility key."""
    manifest_path = os.path.join(folder, "manifest.json")
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not an object")

    folder_real = os.path.realpath(folder)
    artifacts = []
    for item in variants:
        path = ledger_artifact_path(item.get("path"))
        if not path or os.path.dirname(path) != folder_real:
            continue
        artifacts.append({
            "target_mb": item["target_mb"],
            "filename": os.path.basename(path),
            "relative_path": _ledger_relative(path),
            "absolute_path": os.path.abspath(path),
            "size": os.path.getsize(path),
            "created_at": item.get("created_at"),
        })
    artifacts.sort(key=lambda item: item["target_mb"])

    manifest_artifacts = manifest.setdefault("artifacts", {})
    manifest_artifacts["compressed_variants"] = artifacts
    manifest_artifacts["compressed"] = {
        "filename": os.path.basename(latest_path),
        "relative_path": _ledger_relative(latest_path),
        "absolute_path": os.path.abspath(latest_path),
    }
    tmp = manifest_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, manifest_path)
    return manifest_path

def ledger_artifact_path(name):
    """Resolve a relative path without allowing traversal or symlink escape."""
    if not isinstance(name, str) or not name or os.path.isabs(name):
        return None
    path = os.path.realpath(os.path.join(DOWNLOAD_DIR, name))
    if not _path_within_downloads(path):
        return None
    return path if os.path.isfile(path) else None


def receipt_compressed_variants(entry):
    """Return only safe, existing compressed variants from a ledger entry.

    Singular fields from the first compressor release are treated as a one-item
    list, so schema migration is lazy and old receipts keep working.
    """
    if not isinstance(entry, dict):
        return []
    candidates = entry.get("compressed_variants")
    candidates = candidates if isinstance(candidates, list) else []
    legacy = {
        "target_mb": entry.get("compressed_target_mb"),
        "path": entry.get("compressed"),
        "filename": entry.get("compressed_filename"),
        "size": entry.get("compressed_size"),
        "created_at": entry.get("compressed_at"),
    }
    if legacy["path"]:
        candidates = [*candidates, legacy]

    variants, seen = [], set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        target = item.get("target_mb")
        if (
            isinstance(target, bool)
            or not isinstance(target, int)
            or not COMPRESS_MIN_TARGET_MB <= target <= MAX_FILESIZE_MB
        ):
            continue
        relative = item.get("path") or item.get("relative_path")
        path = ledger_artifact_path(relative)
        if not path or target in seen:
            continue
        seen.add(target)
        variants.append({
            "target_mb": target,
            "path": path,
            "relative_path": _ledger_relative(path),
            "filename": os.path.basename(path),
            "size": os.path.getsize(path),
            "created_at": item.get("created_at"),
        })
    return sorted(variants, key=lambda item: item["target_mb"])


def compressed_variant_payloads(entry):
    """The public, path-free representation used by receipt/status APIs."""
    return [
        {key: variant.get(key) for key in
         ("target_mb", "filename", "size", "created_at")}
        for variant in receipt_compressed_variants(entry)
    ]


def compressed_variant(entry, target_mb):
    return next(
        (item for item in receipt_compressed_variants(entry)
         if item["target_mb"] == target_mb),
        None,
    )


def receipt_preview_path(entry):
    path = ledger_artifact_path(entry.get("preview"))
    if path:
        return path
    legacy = ledger_artifact_path(entry.get("file"))
    return legacy if legacy and Path(legacy).suffix.lower() in VIDEO_EXTENSIONS else None


def migrate_legacy_receipts():
    """One-time, non-destructive upgrade from flat files to receipt packages."""
    try:
        with open(LEDGER_PATH) as ledger:
            lines = ledger.readlines()
    except OSError:
        return

    changed = False
    rewritten = []
    root = os.path.realpath(DOWNLOAD_DIR)
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            rewritten.append(raw)
            continue
        job_id = str(entry.get("id", ""))
        name = entry.get("file")
        source = ledger_artifact_path(name)
        if (
            not JOB_ID_RE.match(job_id)
            or not source
            or os.path.basename(name) != name
            or os.path.dirname(os.path.realpath(source)) != root
        ):
            rewritten.append(raw)
            continue

        job = dict(entry)
        job["script_status"] = "done" if entry.get("script") else None
        folder = job_package_dir(job_id, job)
        for key, filename_key in (
            ("file", "filename"),
            ("script", "script_filename"),
            ("transcript", "transcript_filename"),
        ):
            path = ledger_artifact_path(entry.get(key))
            if not path or os.path.dirname(os.path.realpath(path)) != root:
                continue
            moved = finalize_path(path, os.path.join(folder, os.path.basename(path)))
            job[key] = moved
            entry[key] = _ledger_relative(moved)
            entry[filename_key] = os.path.basename(moved)

        if job.get("file"):
            job["files"] = [job["file"]]
            if Path(job["file"]).suffix.lower() in VIDEO_EXTENSIONS:
                job["preview"] = job["file"]
                entry["preview"] = _ledger_relative(job["preview"])
        write_job_manifest(job_id, job)
        entry["folder"] = _ledger_relative(job["folder"])
        entry["manifest"] = _ledger_relative(job["manifest"])
        rewritten.append(json.dumps(entry) + "\n")
        changed = True

    if changed:
        tmp = LEDGER_PATH + ".migration.tmp"
        with ledger_lock:
            with open(tmp, "w") as ledger:
                ledger.writelines(rewritten)
            os.replace(tmp, LEDGER_PATH)


migrate_legacy_receipts()


def image_info(url):
    """Useful metadata without asking yt-dlp to understand an image/gallery page."""
    parsed = urllib.parse.urlsplit(url)
    leaf = urllib.parse.unquote(Path(parsed.path).name)
    stem = Path(leaf).stem.strip() if leaf else ""
    host = (parsed.hostname or "image").removeprefix("www.")
    title = stem or host
    ext = Path(parsed.path).suffix.lower()
    return {
        "title": title,
        "thumbnail": url if ext in IMAGE_EXTENSIONS else "",
        "duration": None,
        "uploader": host,
        "formats": [],
    }


def enforce_media_limits(path):
    """Enforce final byte size and a known, finite media duration."""
    max_bytes = MAX_FILESIZE_MB * 1024 * 1024
    size = os.path.getsize(path)
    if size > max_bytes:
        raise RuntimeError(f"Downloaded file exceeds the {MAX_FILESIZE_MB}MB size limit")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffprobe failed: " + (result.stderr.strip().split("\n")[-1] or "unknown error")
        )
    try:
        duration = float(json.loads(result.stdout or "{}")["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("Could not determine media duration") from None
    if not math.isfinite(duration) or duration < 0:
        raise RuntimeError("Could not determine media duration")
    if duration > MAX_DURATION_S:
        raise RuntimeError(f"Media duration exceeds the {MAX_DURATION_S}s limit")
    return duration


def enforce_file_size(path):
    """Recheck size after any compatibility transcode."""
    if os.path.getsize(path) > MAX_FILESIZE_MB * 1024 * 1024:
        raise RuntimeError(f"Final file exceeds the {MAX_FILESIZE_MB}MB size limit")


def probe_streams(path):
    """Return (video_codec, pix_fmt, audio_codec). Raises on ffprobe failure."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,pix_fmt",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffprobe failed: " + (result.stderr.strip().split("\n")[-1] or "unknown error")
        )
    streams = json.loads(result.stdout or "{}").get("streams", [])
    vcodec, pix_fmt, acodec = "", "", ""
    for s in streams:
        if s.get("codec_type") == "video" and not vcodec:
            vcodec = s.get("codec_name", "")
            pix_fmt = s.get("pix_fmt", "")
        elif s.get("codec_type") == "audio" and not acodec:
            acodec = s.get("codec_name", "")
    return vcodec, pix_fmt, acodec


def ensure_compatible_mp4(path, job_id):
    if not path.endswith(".mp4"):
        return path

    vcodec, pix_fmt, acodec = probe_streams(path)
    if vcodec == "h264" and pix_fmt == "yuv420p" and acodec in ("", "aac"):
        return path

    compat_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_h264.mp4")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", path,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            compat_path,
        ],
        capture_output=True,
        text=True,
        timeout=CONVERT_TIMEOUT_S,
    )
    if result.returncode != 0:
        try:
            os.remove(compat_path)
        except OSError:
            pass
        raise RuntimeError(result.stderr.strip().split("\n")[-1] or "Failed to convert video")

    try:
        os.remove(path)
    except OSError:
        pass
    return compat_path


# ── compress to fit · a size cap is a bitrate budget ────────────────────────
def probe_compress_source(path):
    """(duration, height, has_audio) for one file. Raises on ffprobe failure."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,height",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffprobe failed: " + (result.stderr.strip().split("\n")[-1] or "unknown error")
        )
    probe = json.loads(result.stdout or "{}")
    try:
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Could not determine the video's duration") from None
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("Could not determine the video's duration")
    height, has_audio = 0, False
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video" and not height:
            try:
                height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                height = 0
        elif stream.get("codec_type") == "audio":
            has_audio = True
    return duration, height, has_audio


def compress_scale_height(video_kbps, source_height):
    """The height to encode at, or None to keep the source resolution. Only ever
    downscales: upscaling would spend the budget inventing pixels."""
    for max_kbps, height in COMPRESS_HEIGHT_LADDER:
        if video_kbps < max_kbps:
            return height if source_height and source_height > height else None
    return None


def compress_floor_mb(duration, has_audio):
    """The smallest cap this duration can be encoded into and stay watchable."""
    kbps = COMPRESS_MIN_VIDEO_KBPS + (COMPRESS_AUDIO_LADDER[-1] if has_audio else 0)
    return max(
        COMPRESS_MIN_TARGET_MB,
        math.ceil(kbps * 1000 * duration / 8 / (1024 * 1024) / COMPRESS_SAFETY),
    )


def compress_plan(target_mb, duration, height, has_audio):
    """(video_kbps, audio_kbps, scale_height) for a cap over a duration.

    The cap is a byte budget spread across the runtime. Audio takes a fixed
    slice off the top and video gets the rest; when the rest would be too thin
    to look like video at all, audio steps down the ladder before quality does.
    """
    budget_kbps = (target_mb * 1024 * 1024 * 8) / duration / 1000 * COMPRESS_SAFETY
    for audio_kbps in (COMPRESS_AUDIO_LADDER if has_audio else (0,)):
        video_kbps = int(budget_kbps - audio_kbps)
        if video_kbps >= COMPRESS_MIN_VIDEO_KBPS:
            return video_kbps, audio_kbps, compress_scale_height(video_kbps, height)
    floor_mb = compress_floor_mb(duration, has_audio)
    raise RuntimeError(
        f"{target_mb}MB is too small for {round(duration)}s of video — "
        f"the smallest workable cap here is about {floor_mb}MB"
    )


def run_ffmpeg_pass(args, deadline, label):
    """One ffmpeg pass inside the compression's shared time budget, so two passes
    plus a corrective one cannot add up to three times the timeout."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(f"Compression timed out after {COMPRESS_TIMEOUT_S}s")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Compression timed out after {COMPRESS_TIMEOUT_S}s") from None
    if result.returncode != 0:
        detail = result.stderr.strip().split("\n")[-1] if result.stderr.strip() else ""
        raise RuntimeError(f"{label} failed: {detail or 'ffmpeg reported no reason'}")
    return result


def compress_to_target(src, work_dir, target_mb, on_pass=None):
    """Two-pass libx264 encode of `src` into `work_dir` under target_mb.

    Returns (path, size, video_kbps, audio_kbps, scale_height). The caller owns
    work_dir and is responsible for removing it.
    """
    duration, height, has_audio = probe_compress_source(src)
    video_kbps, audio_kbps, scale_h = compress_plan(target_mb, duration, height, has_audio)
    cap = target_mb * 1024 * 1024
    log = os.path.join(work_dir, "x264")
    out = os.path.join(work_dir, "compressed.mp4")
    deadline = time.monotonic() + COMPRESS_TIMEOUT_S

    def video_args(kbps):
        args = ["-c:v", "libx264", "-preset", COMPRESS_PRESET, "-b:v", f"{kbps}k"]
        if scale_h:
            # -2 keeps the aspect ratio and an even width, which yuv420p requires.
            args += ["-vf", f"scale=-2:{scale_h}"]
        return args + ["-pix_fmt", "yuv420p", "-passlogfile", log]

    if on_pass:
        on_pass(1)
    run_ffmpeg_pass(
        ["ffmpeg", "-y", "-i", src, "-map", "0:v:0"]
        + video_args(video_kbps)
        + ["-pass", "1", "-an", "-f", "mp4", os.devnull],
        deadline,
        "Analysis pass",
    )

    if on_pass:
        on_pass(2)
    size = 0
    for attempt in (1, 2):
        run_ffmpeg_pass(
            ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?"]
            + video_args(video_kbps)
            + ["-pass", "2", "-c:a", "aac", "-b:a", f"{max(audio_kbps, 32)}k"]
            + ["-movflags", "+faststart", out],
            deadline,
            "Encode pass",
        )
        size = os.path.getsize(out)
        if size <= cap or attempt == 2:
            break
        # Two-pass normally lands within a percent or two, but a stubborn source
        # can still overshoot. Pass 1 measured per-frame complexity, not bitrate,
        # so its log stays valid at a lower one: only pass 2 is re-run, scaled by
        # exactly how far it missed. The scale height is deliberately not
        # replanned — changing resolution would invalidate that log.
        video_kbps = max(COMPRESS_MIN_VIDEO_KBPS, int(video_kbps * cap / size * COMPRESS_SAFETY))

    if size > cap:
        raise RuntimeError(
            f"Could not get this video under {target_mb}MB — "
            f"the closest was {size / (1024 * 1024):.1f}MB"
        )
    return out, size, video_kbps, audio_kbps, scale_h


def format_transcript(sentences):
    sentences = [s for s in sentences if s.get("text", "").strip()]
    script = "\n".join(s["text"].strip() for s in sentences).strip() + "\n"
    timed = "\n".join(
        f"[{float(s.get('start', 0)):06.2f}-{float(s.get('end', 0)):06.2f}] {s['text'].strip()}"
        for s in sentences
    ).strip() + "\n"
    return script, timed


def transcribe_download(path, job_id):
    if not os.path.exists(TRANSCRIBE_BIN):
        raise RuntimeError(f"Transcription engine not found: {TRANSCRIBE_BIN}")

    media = Path(path)
    stem = media.with_suffix("")
    script_path = Path(f"{stem}.script.txt")
    transcript_path = Path(f"{stem}.timestamps.txt")
    work_dir = Path(DOWNLOAD_DIR) / ".transcribe-work" / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        wav = work_dir / "audio16k.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", str(media),
                "-ac", "1",
                "-ar", "16000",
                "-c:a", "pcm_s16le",
                str(wav),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        with transcribe_slot:
            subprocess.run(
                [
                    TRANSCRIBE_BIN,
                    str(wav),
                    "--output-format", "json",
                    "--output-dir", str(work_dir),
                    "--output-template", "parakeet",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=TRANSCRIBE_TIMEOUT_S,
            )

        raw = json.loads((work_dir / "parakeet.json").read_text())
        script, timed = format_transcript(raw.get("sentences", []))
        script_path.write_text(script)
        transcript_path.write_text(timed)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "script": str(script_path),
        "script_filename": script_path.name,
        "transcript": str(transcript_path),
        "transcript_filename": transcript_path.name,
    }


class DownloadStalled(RuntimeError):
    """A downloader stayed alive without changing any job-owned file."""


def progress_signature(patterns):
    """Stable snapshot of job-scoped files used to distinguish slow from stuck.

    Patterns may be recursive globs. Size catches normal writes; mtime catches a
    downloader rewriting fixed-size blocks. Missing and racing files are ignored.
    """
    signature = []
    seen = set()
    for pattern in patterns:
        for name in glob.glob(os.fspath(pattern), recursive=True):
            path = os.path.realpath(name)
            if path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            signature.append((path, stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(signature))


def terminate_process_tree(process):
    """Stop a downloader and children such as ffmpeg, then reap all pipes."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.communicate()


def run_download_command(
    cmd,
    progress_patterns,
    *,
    timeout=DOWNLOAD_TIMEOUT_S,
    stall_timeout=DOWNLOAD_STALL_S,
    poll_interval=None,
):
    """Run a downloader with both an overall limit and a no-progress watchdog.

    The watchdog observes only files owned by this job, so another concurrent
    download cannot keep a stuck process alive. Set the stall timeout to 0 to
    disable it and retain subprocess.run semantics for unusual environments.
    """
    check_cancel()
    if stall_timeout <= 0:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    poll_interval = poll_interval or min(1.0, max(0.05, stall_timeout / 4))
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    started = time.monotonic()
    last_progress = started
    last_signature = progress_signature(progress_patterns)

    try:
        while True:
            check_cancel()
            elapsed = time.monotonic() - started
            remaining = timeout - elapsed
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(poll_interval, remaining))
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                signature = progress_signature(progress_patterns)
                if signature != last_signature:
                    last_signature = signature
                    last_progress = now
                if now - started >= timeout:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                if now - last_progress >= stall_timeout:
                    raise DownloadStalled(
                        f"Download stalled — no file progress for {stall_timeout:g}s; retry the job"
                    )
    except BaseException:
        terminate_process_tree(process)
        raise


def ytdlp_base():
    """Every yt-dlp invocation starts here. IPv4 is forced by default: a broken
    IPv6 path (e.g. a VPN owning the v6 default route) makes CDN transfers stall
    silently until the timeout instead of erroring. FRAMEGRAB_FORCE_IPV4=0 opts out."""
    if _env("FORCE_IPV4", "1").strip().lower() in ("0", "false", "no"):
        return ["yt-dlp"]
    return ["yt-dlp", "--force-ipv4"]


def build_fetch_command(job_id, url, format_choice, format_id):
    """The yt-dlp invocation for one intake job. `--` guarantees the URL can
    never be read as an option; size/duration filters are early hints only —
    enforce_media_limits() is the authority on the produced file."""
    cmd = ytdlp_base() + [
        "--no-playlist",
        "-o", os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s"),
        "--max-filesize", f"{MAX_FILESIZE_MB}M",
        "--match-filter", f"duration <=? {MAX_DURATION_S}",
    ]
    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    else:
        selector = f"{format_id}+bestaudio/best" if format_id else "bestvideo+bestaudio/best"
        cmd += ["-f", selector, "--merge-output-format", "mp4"]
    return cmd + ["--", url]


def instagram_post(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    return (host == 'instagram.com' or host.endswith('.instagram.com')) and bool(
        re.match(r'^/(?:p|reel|reels)/[^/]+', parsed.path)
    )


def gallery_browser_args(url):
    """Explicit local opt-in; never export or persist a browser cookie jar."""
    browser = _env('COOKIES_FROM_BROWSER', '').strip().lower()
    if not browser or not instagram_post(url):
        return []
    if browser not in {'chrome', 'chromium', 'firefox', 'safari', 'edge', 'brave'}:
        raise RuntimeError('FRAMEGRAB_COOKIES_FROM_BROWSER must name a supported browser')
    return ['--cookies-from-browser', browser]


def build_image_command(job_id, url, include_video=False):
    """A config-independent, bounded gallery-dl invocation for image assets."""
    work_dir = os.path.join(DOWNLOAD_DIR, ".image-work", job_id)
    cmd = [
        "gallery-dl",
        "--config-ignore",
        "--no-input",
        "--no-part",
        "--no-mtime",
        "--directory", work_dir,
        "--range", f"1-{MAX_IMAGES_PER_JOB}",
        "--filesize-max", f"{MAX_FILESIZE_MB if include_video else MAX_IMAGE_FILESIZE_MB}M",
    ]
    extensions = sorted(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS if include_video else IMAGE_EXTENSIONS)
    cmd += ['--filter', 'extension in ' + repr(tuple(ext.lstrip('.') for ext in extensions))]
    if include_video:
        # Instagram's num is the original sidecar position, not a media-class index.
        cmd += ['--filename', '{num:03}.{extension}']
    return cmd + gallery_browser_args(url) + ['--', url]


def collect_image_outputs(work_dir, include_video=False):
    """Return safe image files from a gallery-dl work tree, enforcing limits."""
    root = Path(work_dir).resolve()
    candidates = []
    total = 0
    per_file_max = MAX_IMAGE_FILESIZE_MB * 1024 * 1024
    total_max = MAX_FILESIZE_MB * 1024 * 1024
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise RuntimeError("Image downloader produced an unsafe path")
        allowed = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS if include_video else IMAGE_EXTENSIONS
        if path.suffix.lower() not in allowed:
            continue
        size = path.stat().st_size
        if path.suffix.lower() in IMAGE_EXTENSIONS and size > per_file_max:
            raise RuntimeError(
                f"An image exceeds the {MAX_IMAGE_FILESIZE_MB}MB per-image limit"
            )
        total += size
        if total > total_max:
            raise RuntimeError(f"Image assets exceed the {MAX_FILESIZE_MB}MB job limit")
        candidates.append(path)

    if not candidates:
        raise RuntimeError("No supported image assets were found at this URL")
    if len(candidates) > MAX_IMAGES_PER_JOB:
        raise RuntimeError(f"Image job exceeds the {MAX_IMAGES_PER_JOB}-asset limit")
    return candidates


def download_image_assets(job_id, url, include_video=False):
    """Download and finalize numbered images, returning their local paths."""
    job = jobs[job_id]
    work_dir = Path(DOWNLOAD_DIR) / ".image-work" / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    finalized = []
    package_dir = job_package_dir(job_id, job)
    try:
        result = run_download_command(
            build_image_command(job_id, url, include_video=include_video),
            [os.path.join(str(work_dir), "**", "*")],
        )
        if result.returncode != 0:
            raise RuntimeError(classify_download_error(result.stderr))

        outputs = collect_image_outputs(work_dir, include_video=include_video)
        title = job.get("title", "").strip()
        width = max(2, len(str(len(outputs))))
        for index, src in enumerate(outputs, 1):
            if src.suffix.lower() in VIDEO_EXTENSIONS:
                enforce_media_limits(str(src))
                src = Path(ensure_compatible_mp4(str(src), f'{job_id}-{index:03}'))
                # Register intermediate ownership so a failed finalization cleans it up.
                finalized.append(str(src))
                enforce_file_size(str(src))
            numbered_title = title if len(outputs) == 1 else f"{title}-{index:0{width}d}"
            fallback = job_id if len(outputs) == 1 else f"{job_id}-{index:0{width}d}"
            wanted = safe_download_name(numbered_title, src.suffix.lower(), fallback)
            destination = finalize_path(str(src), os.path.join(package_dir, wanted))
            if str(src) in finalized:
                finalized.remove(str(src))
            finalized.append(destination)

        return finalized
    except Exception:
        for path in finalized:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def file_asset_receipt(job, assets, job_id, archive_ext):
    """Expose source assets individually and use a ZIP as the multi-asset receipt."""
    job["files"] = assets
    job["filenames"] = [os.path.basename(path) for path in assets]
    job["file_count"] = len(assets)
    if len(assets) == 1:
        job["file"] = assets[0]
        job["filename"] = os.path.basename(assets[0])
        return

    package_dir = job_package_dir(job_id, job)
    archive_tmp = os.path.join(DOWNLOAD_DIR, f"{job_id}{archive_ext}")
    with zipfile.ZipFile(archive_tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in assets:
            archive.write(path, arcname=os.path.basename(path))
    title = job.get("title", "").strip()
    archive_name = safe_download_name(title, archive_ext, job_id)
    archive_path = finalize_path(archive_tmp, os.path.join(package_dir, archive_name))
    job["file"] = archive_path
    job["filename"] = os.path.basename(archive_path)


def run_image_download(job_id, url):
    """Download and file one image or a numbered gallery plus a ZIP bundle."""
    job = jobs[job_id]
    assets = download_image_assets(job_id, url)
    try:
        file_asset_receipt(job, assets, job_id, ".images.zip")
        job["asset_types"] = ["image"] * len(assets)
        job["detected_format"] = "image"
        job["limit_reached"] = len(assets) == MAX_IMAGES_PER_JOB
    except Exception:
        for path in assets:
            try:
                os.remove(path)
            except OSError:
                pass
        raise


def pick_output(job_id, format_choice):
    """The file this job produced, preferring the requested container."""
    produced = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
    if not produced:
        return None
    wanted_ext = ".mp3" if format_choice == "audio" else ".mp4"
    matching = [f for f in produced if f.endswith(wanted_ext)]
    chosen = matching[0] if matching else produced[0]
    for leftover in produced:
        if leftover != chosen:
            try:
                os.remove(leftover)
            except OSError:
                pass
    return chosen


def attach_transcripts(job, media_path, job_id):
    """Best-effort script/timestamps receipts; media survives a script failure."""
    job["script_status"] = "running"
    job["status"] = "transcribing"
    try:
        job.update(transcribe_download(media_path, job_id))
        job["script_status"] = "done"
    except Exception as e:
        job["script_status"] = "error"
        job["script_error"] = str(e).strip().split("\n")[-1]


def download_media_asset(job_id, url, format_choice, format_id, allow_audio=False):
    """Fetch, verify, normalize, and file one video/audio asset."""
    job = jobs[job_id]
    result = run_download_command(
        build_fetch_command(job_id, url, format_choice, format_id),
        [
            os.path.join(DOWNLOAD_DIR, f"{job_id}.*"),
            os.path.join(DOWNLOAD_DIR, f"{job_id}_h264.*"),
        ],
    )
    if result.returncode != 0:
        raise RuntimeError(classify_download_error(result.stderr))

    media = pick_output(job_id, format_choice)
    if media is None:
        raise RuntimeError(
            "No file was produced (media may exceed the "
            f"{MAX_DURATION_S}s duration or {MAX_FILESIZE_MB}MB size limit)"
        )
    output_ext = Path(media).suffix.lower()
    if format_choice == "video" and output_ext in IMAGE_EXTENSIONS:
        raise RuntimeError("Source contains an image rather than a video")
    detected_type = "audio" if output_ext in AUDIO_EXTENSIONS else format_choice
    if format_choice == "video" and detected_type == "audio" and not allow_audio:
        raise RuntimeError("Source contains audio rather than a video")

    enforce_media_limits(media)
    if detected_type == "video":
        media = ensure_compatible_mp4(media, job_id)
        enforce_file_size(media)

    ext = os.path.splitext(media)[1]
    wanted = safe_download_name(job.get("title", "").strip(), ext, job_id)
    media = finalize_path(media, os.path.join(job_package_dir(job_id, job), wanted))
    if detected_type == "video":
        job["preview"] = media
    return media, detected_type


def run_auto_download(job_id, url):
    """Extract every source media class available: video plus gallery images."""
    job = jobs[job_id]
    if instagram_post(url) and urllib.parse.urlsplit(url).path.startswith("/p/"):
        # One ordered extraction, not one video followed by an image-only pass.
        assets = download_image_assets(job_id, url, include_video=True)
        file_asset_receipt(job, assets, job_id, '.assets.zip')
        kinds = ['video' if Path(path).suffix.lower() in VIDEO_EXTENSIONS else 'image' for path in assets]
        job['asset_types'] = kinds
        job['detected_format'] = kinds[0] if len(set(kinds)) == 1 else 'mixed'
        job['limit_reached'] = len(assets) == MAX_IMAGES_PER_JOB
        video = next((path for path, kind in zip(assets, kinds) if kind == 'video'), None)
        if video:
            job['preview'] = video
        return
    primary = None
    primary_type = None
    images = []
    errors = []

    direct_ext = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    if direct_ext not in IMAGE_EXTENSIONS:
        try:
            primary, primary_type = download_media_asset(
                job_id, url, "video", None, allow_audio=True,
            )
        except JobCancelled:
            raise
        except Exception as exc:
            cleanup_job_artifacts(job_id)
            errors.append(str(exc))

    job["status"] = "downloading"
    try:
        images = [] if primary and instagram_post(url) else download_image_assets(job_id, url)
    except JobCancelled:
        raise
    except Exception as exc:
        errors.append(str(exc))

    assets = ([primary] if primary else []) + images
    if not assets:
        detail = '; '.join(message for message in errors if "unsupported" not in message.lower())
        raise RuntimeError(detail or "No downloadable media assets were found at this URL")

    try:
        archive_ext = ".assets.zip" if primary and images else ".images.zip"
        file_asset_receipt(job, assets, job_id, archive_ext)
    except Exception:
        receipts = [job.get("script"), job.get("transcript")]
        for path in assets + [path for path in receipts if path]:
            try:
                os.remove(path)
            except OSError:
                pass
        raise

    job["asset_types"] = ([primary_type] if primary else []) + ["image"] * len(images)
    job["detected_format"] = "mixed" if primary and images else (primary_type or "image")
    job["limit_reached"] = len(images) == MAX_IMAGES_PER_JOB


def finish_receipt(job_id, job):
    """Preserve media, but report incomplete metadata as a repairable error."""
    failures = []
    for label, write in (("manifest", write_job_manifest), ("history", record_receipt)):
        try:
            job["package_status"] = "error" if failures else "done"
            write(job_id, job)
        except Exception as exc:
            app.logger.exception("Receipt %s: cannot save %s", job_id, label)
            failures.append(f"{label}: {exc}")
    job['package_status'] = 'error' if failures else 'done'
    if failures:
        job.update(status='error', error_kind='metadata',
                   error='Media saved; receipt needs repair. ' + '; '.join(failures))
    else:
        job.update(status='done', error=None, error_kind=None)
    return not failures


def run_download(job_id, url, format_choice, format_id):
    """Acquire once, checkpoint media, enrich it, then commit the handoff."""
    job = jobs[job_id]
    worker_context.job_id = job_id
    with jobs_lock:
        active_workers.add(job_id)
    try:
        with download_slots:
            check_cancel()
            if not job.get('acquired'):
                throttle_domain(url)
                check_cancel()
                job['status'] = 'downloading'
                checkpoint(job_id)
                if format_choice == 'auto':
                    run_auto_download(job_id, url)
                elif format_choice == 'image':
                    run_image_download(job_id, url)
                else:
                    media, detected_type = download_media_asset(job_id, url, format_choice, format_id)
                    file_asset_receipt(job, [media], job_id, '.assets.zip')
                    job['asset_types'] = [detected_type]
                    job['detected_format'] = detected_type
                job['acquired'] = True
                checkpoint(job_id)
            check_cancel()
            if TRANSCRIBE_ENABLED and job.get('preview') and job.get('script_status') != 'done':
                job['status'] = 'transcribing'
                checkpoint(job_id)
                attach_transcripts(job, job['preview'], job_id)
                checkpoint(job_id)
            check_cancel()
            finish_receipt(job_id, job)
    except JobCancelled as exc:
        job.update(status='error', error_kind='cancelled', error=str(exc))
    except Exception as exc:
        # An interrupted enrichment must never discard acquired source media.
        if not job.get('acquired'):
            cleanup_job_artifacts(job_id)
        job.update(status='error', error_kind='download', error=str(exc))
    finally:
        job['finished_at'] = time.time()
        try:
            checkpoint(job_id)
        except Exception:
            app.logger.exception('Cannot checkpoint job %s', job_id)
            job.update(status='error', error_kind='persistence', error='Unable to persist job state. Check available disk space.')
        worker_context.job_id = None
        with jobs_lock:
            active_workers.discard(job_id)


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(413)
def payload_too_large(e):
    return jsonify({"error": "Request body too large"}), 413


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error"}), 500


@app.route("/")
def index():
    return render_template("index.html", transcribe_enabled=TRANSCRIBE_ENABLED)


def quality_options(raw_formats):
    """One selectable option per resolution: the highest-bitrate video format
    at each height, with ids screened so only shell-safe values reach clients."""
    best = {}
    for fmt in raw_formats:
        height = fmt.get("height")
        if not height or fmt.get("vcodec", "none") == "none":
            continue
        incumbent = best.get(height)
        if incumbent is None or (fmt.get("tbr") or 0) > (incumbent.get("tbr") or 0):
            best[height] = fmt

    options = [
        {"id": str(fmt.get("format_id", "")), "label": f"{height}p", "height": height}
        for height, fmt in best.items()
        if FORMAT_ID_RE.match(str(fmt.get("format_id", "")))
    ]
    options.sort(key=lambda opt: opt["height"], reverse=True)
    return options


@app.route("/api/info", methods=["POST"])
def get_info():
    data, err = parse_json_body()
    if err:
        return err
    url = validate_url(data.get("url"))
    if not url:
        return jsonify({"error": "A valid http(s) URL is required"}), 400

    requested_format = data.get("format", "video")
    if requested_format == "image":
        return jsonify(image_info(url))
    if requested_format == "auto" and Path(urllib.parse.urlsplit(url).path).suffix.lower() in IMAGE_EXTENSIONS:
        return jsonify(image_info(url))

    try:
        result = subprocess.run(
            ytdlp_base() + ["--no-playlist", "-j", "--", url],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0 and requested_format == "auto":
            return jsonify(image_info(url))
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": quality_options(info.get("formats", [])),
        })
    except subprocess.TimeoutExpired:
        if requested_format == "auto":
            return jsonify(image_info(url))
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        if requested_format == "auto":
            return jsonify(image_info(url))
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data, err = parse_json_body()
    if err:
        return err
    url = validate_url(data.get("url"))
    if not url:
        return jsonify({"error": "A valid http(s) URL is required"}), 400

    cmd = ytdlp_base() + [
        "--flat-playlist",
        "-J",
        "--playlist-items", f"1:{MAX_PLAYLIST_ITEMS}",
        "--", url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if validate_url(entry.get("url"))]
        truncated = len(entries) >= MAX_PLAYLIST_ITEMS
        return jsonify({"urls": urls[:MAX_PLAYLIST_ITEMS], "truncated": truncated})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    with jobs_lock:
        return admit_download()


def admit_download():
    prune_jobs()
    data, err = parse_json_body()
    if err:
        return err
    url = validate_url(data.get("url"))
    if not url:
        return jsonify({"error": "A valid http(s) URL is required"}), 400

    format_choice = data.get("format", "auto")
    if format_choice not in ("auto", "video", "audio", "image"):
        return jsonify({"error": "format must be 'auto', 'video', 'audio', or 'image'"}), 400

    format_id = data.get("format_id")
    if format_id is not None:
        format_id = str(format_id)
        if not FORMAT_ID_RE.match(format_id):
            return jsonify({"error": "Invalid format_id"}), 400

    title = data.get("title", "")
    if not isinstance(title, str):
        return jsonify({"error": "title must be a string"}), 400

    # Optional display metadata the client already fetched via /api/info;
    # persisted to the ledger only, never interpreted server-side.
    uploader = data.get("uploader", "")
    if not isinstance(uploader, str):
        uploader = ""
    duration = data.get("duration")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        duration = None
    thumbnail = data.get("thumbnail", "")
    if not isinstance(thumbnail, str) or not thumbnail.startswith(("http://", "https://")) or len(thumbnail) > 2048:
        thumbnail = ""

    request_key = request.headers.get('Idempotency-Key') or data.get('request_id')
    if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128):
        return jsonify({'error': 'request_id must be a string of 1–128 characters'}), 400
    identity = [url, format_choice, format_id]
    with jobs_lock:
        existing = store.get_request(request_key)
        if existing:
            existing_id, old = existing
            if old.get('request_identity') != identity:
                return jsonify({'error': 'Idempotency key already belongs to another request'}), 409
            jobs.setdefault(existing_id, old)
            return jsonify({'job_id': existing_id, 'reused': True})
    if free_disk_mb() < MIN_FREE_DISK_MB:
        return jsonify({"error": "Insufficient disk space for new downloads"}), 507

    job_id = uuid.uuid4().hex[:10]
    if not reserve_job(job_id, {
        "status": "queued", "url": url, "title": title[:300], "format": format_choice,
        "format_id": format_id, "request_key": request_key, "request_identity": identity,
        "uploader": uploader[:200], "duration": duration, "thumbnail": thumbnail,
    }):
        return jsonify({"error": f"Too many active jobs (limit {MAX_ACTIVE_JOBS}) — retry later"}), 429

    try:
        thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
        thread.daemon = True
        thread.start()
    except Exception:
        with jobs_lock:
            jobs[job_id].update(status='error', error_kind='worker', error='Unable to start worker')
            checkpoint(job_id)
        raise

    return jsonify({"job_id": job_id})


@app.route('/api/health')
def health():
    return jsonify(status='ok', version='2', build_id=BUILD_ID, active_jobs=active_job_count(),
                   downloads_folder=DOWNLOAD_DIR, transcription_enabled=TRANSCRIBE_ENABLED,
                   capabilities=['durable-jobs', 'receipt-search', 'asset-detail', 'frame-capture'])


@app.route('/api/jobs')
def list_jobs():
    # Persisted errors stay discoverable until dismissed; successes live in History.
    with jobs_lock:
        visible = [{**job, 'job_id': job_id} for job_id, job in jobs.items()
                   if job.get('status') != 'done' and not job.get('dismissed')]
    return jsonify(jobs=sorted(visible, key=lambda j: j.get('created_at', 0)))


@app.route('/api/jobs/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error='Job not found'), 404
        if job.get('status') in TERMINAL_STATUSES:
            return jsonify(error='Job has already finished'), 409
        job['cancel_requested'] = True
        checkpoint(job_id)
    return jsonify(ok=True, message='Cancelling at the next safe boundary; saved media will be kept.')


@app.route('/api/jobs/<job_id>/dismiss', methods=['POST'])
def dismiss_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get('status') not in TERMINAL_STATUSES:
            return jsonify(error='Only finished jobs can be dismissed'), 409
        job['dismissed'] = True
        checkpoint(job_id)
    return jsonify(ok=True)


@app.route('/api/jobs/<job_id>/retry', methods=['POST'])
def retry_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id) or store.get_job(job_id)
        if not job:
            return jsonify(error='Job not found'), 404
        if job.get('status') != 'error' or job_id in active_workers:
            return jsonify(error='Only failed or interrupted jobs can be retried'), 409
        if active_job_count() >= MAX_ACTIVE_JOBS:
            return jsonify(error='Queue full; retry when a slot is available'), 429
        if free_disk_mb() < MIN_FREE_DISK_MB:
            return jsonify(error='Insufficient disk space'), 507
        if job.get('acquired') and not all(os.path.isfile(p) for p in job.get('files', [job.get('file', '')])):
            return jsonify(error='Saved source media is missing. Submit the URL again.'), 409
        jobs[job_id] = job
        job.update(status='queued', error=None, error_kind=None, cancel_requested=False, dismissed=False)
        checkpoint(job_id)
        try:
            threading.Thread(target=run_download, args=(job_id, job['url'], job['format'], job.get('format_id')), daemon=True).start()
        except Exception:
            job.update(status='error', error_kind='worker', error='Unable to start worker')
            checkpoint(job_id)
            raise
    return jsonify(job_id=job_id)


@app.route("/api/status/<job_id>")
def check_status(job_id):
    prune_jobs()
    job = jobs.get(job_id) or store.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    jobs.setdefault(job_id, job)
    artifact_paths = {
        "folder": job.get("folder"),
        "media": job.get("preview") or job.get("file"),
        "download": job.get("file"),
        "script": job.get("script"),
        "timestamps": job.get("transcript"),
        "manifest": job.get("manifest"),
        "frames": job.get("frames_dir"),
    }
    return jsonify({
        "status": job["status"],
        "title": job.get("title", ""),
        "creator": job.get("uploader", ""),
        "source_url": job.get("url"),
        "error": job.get("error"),
        "error_kind": job.get("error_kind"),
        "cancel_requested": job.get("cancel_requested", False),
        "package_status": job.get("package_status"),
        "media_saved": bool(job.get("file") and os.path.isfile(job["file"])),
        "filename": job.get("filename"),
        "filenames": job.get("filenames"),
        "file_count": job.get("file_count"),
        "asset_types": job.get("asset_types"),
        "detected_format": job.get("detected_format"),
        "limit_reached": job.get("limit_reached", False),
        "script_status": job.get("script_status"),
        "script_error": job.get("script_error"),
        "script_filename": job.get("script_filename"),
        "transcript_filename": job.get("transcript_filename"),
        "folder_name": job.get("folder_name"),
        "artifact_paths": artifact_paths,
        "preview_available": bool(job.get("preview") and os.path.exists(job["preview"])),
        "auto_saved": job.get("status") == "done",
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    # Media is intentionally available as soon as the file is finalized,
    # even while transcription is still running.
    if not job or not job.get("file") or not os.path.exists(job["file"]):
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/preview/<job_id>")
def preview_file(job_id):
    job = jobs.get(job_id)
    path = job.get("preview") if job else None
    if not path or not os.path.exists(path):
        return jsonify({"error": "Preview not ready"}), 404
    return send_file(path, as_attachment=False, conditional=True)


@app.route("/api/reveal/<job_id>", methods=["POST"])
def reveal_job_artifact(job_id):
    if not JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    job = jobs.get(job_id)
    path = (job.get("preview") or job.get("file")) if job else None
    return reveal_response(path)


@app.route("/api/file/<job_id>/<int:index>")
def download_source_asset(job_id, index):
    job = jobs.get(job_id)
    files = job.get("files", []) if job else []
    if index < 1 or index > len(files) or not os.path.exists(files[index - 1]):
        return jsonify({"error": "Source asset not ready"}), 404
    path = files[index - 1]
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/api/script/<job_id>")
def download_script(job_id):
    job = jobs.get(job_id)
    if not job or job.get("script_status") != "done" or not os.path.exists(job.get("script", "")):
        return jsonify({"error": "Script not ready"}), 404
    return send_file(
        job["script"],
        as_attachment=request.args.get("view") != "1",
        download_name=job["script_filename"],
    )


@app.route("/api/transcript/<job_id>")
def download_transcript(job_id):
    job = jobs.get(job_id)
    if not job or job.get("script_status") != "done" or not os.path.exists(job.get("transcript", "")):
        return jsonify({"error": "Transcript not ready"}), 404
    return send_file(
        job["transcript"],
        as_attachment=request.args.get("view") != "1",
        download_name=job["transcript_filename"],
    )


# ── ledger · receipts that survive restarts ─────────────────────────────────
def receipt_folder_path(entry):
    relative = entry.get("folder")
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        return None
    path = os.path.realpath(os.path.join(DOWNLOAD_DIR, relative))
    if not _path_within_downloads(path) or not os.path.isdir(path):
        return None
    return path


def find_receipt(entry_id):
    """The ledger entry with this id, or None."""
    return next((e for e in load_ledger() if e.get("id") == entry_id), None)


@app.route("/api/receipts")
def list_receipts():
    receipts = []
    for entry in load_ledger():
        if not JOB_ID_RE.match(str(entry.get("id", ""))):
            continue
        variants = compressed_variant_payloads(entry)
        latest = variants[-1] if variants else None
        receipts.append({
            "id": entry["id"],
            "saved_at": entry.get("saved_at"),
            "url": entry.get("url"),
            "title": entry.get("title", ""),
            "uploader": entry.get("uploader", ""),
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail", ""),
            "detected_format": entry.get("detected_format"),
            "file_count": entry.get("file_count"),
            "filename": entry.get("filename"),
            "has_script": bool(entry.get("script")),
            "package_status": entry.get("package_status", "done"),
            "has_transcript": bool(entry.get("transcript")),
            "folder": receipt_folder_path(entry),
            "folder_name": os.path.basename(entry.get("folder") or ""),
            "has_preview": bool(receipt_preview_path(entry)),
            # Reported from the ledger, not live state, so every saved cap
            # survives restarts and remains discoverable on the receipt.
            "has_compressed": bool(variants),
            "compressed_variants": variants,
            # Compatibility fields name the most recently written variant.
            "compressed_filename": (
                entry.get("compressed_filename")
                or (latest or {}).get("filename")
            ),
            "compressed_size": (
                entry.get("compressed_size")
                or (latest or {}).get("size")
            ),
        })
    return jsonify({"downloads_folder": DOWNLOAD_DIR, "receipts": receipts})


@app.route("/api/receipts/<entry_id>", methods=["DELETE"])
def delete_receipt(entry_id):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    if not remove_ledger_entry(entry_id):
        return jsonify({"error": "Receipt not found"}), 404
    return jsonify({"ok": True})


def send_ledger_artifact(entry_id, key, filename_key):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    entry = find_receipt(entry_id)
    path = ledger_artifact_path(entry.get(key)) if entry else None
    if not path:
        return jsonify({"error": "Receipt artifact not found"}), 404
    return send_file(
        path,
        as_attachment=request.args.get("view") != "1",
        download_name=entry.get(filename_key) or os.path.basename(path),
    )


@app.route("/api/receipts/<entry_id>/preview")
def receipt_preview(entry_id):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    entry = find_receipt(entry_id)
    path = receipt_preview_path(entry) if entry else None
    if not path:
        return jsonify({"error": "Preview not found"}), 404
    return send_file(path, as_attachment=False, conditional=True)


@app.route("/api/receipts/<entry_id>/reveal", methods=["POST"])
def reveal_receipt_artifact(entry_id):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    entry = find_receipt(entry_id)
    path = receipt_preview_path(entry) if entry else None
    if not path and entry:
        path = ledger_artifact_path(entry.get("file"))
    return reveal_response(path)


@app.route("/api/receipts/<entry_id>/file")
def receipt_file(entry_id):
    return send_ledger_artifact(entry_id, "file", "filename")


@app.route("/api/receipts/<entry_id>/script")
def receipt_script(entry_id):
    return send_ledger_artifact(entry_id, "script", "script_filename")


@app.route("/api/receipts/<entry_id>/transcript")
def receipt_transcript(entry_id):
    return send_ledger_artifact(entry_id, "transcript", "transcript_filename")


@app.route("/api/receipts/<entry_id>/compressed")
def receipt_compressed(entry_id):
    return send_ledger_artifact(entry_id, "compressed", "compressed_filename")


@app.route("/api/receipts/<entry_id>/compressed/reveal", methods=["POST"])
def reveal_receipt_compressed(entry_id):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    entry = find_receipt(entry_id)
    path = ledger_artifact_path(entry.get("compressed")) if entry else None
    return reveal_response(path)


@app.route("/api/receipts/<entry_id>/compressed/<int:target_mb>")
def receipt_compressed_variant(entry_id, target_mb):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    entry = find_receipt(entry_id)
    variant = compressed_variant(entry, target_mb) if entry else None
    if not variant:
        return jsonify({"error": "Compressed variant not found"}), 404
    return send_file(
        variant["path"],
        as_attachment=request.args.get("view") != "1",
        download_name=variant["filename"],
    )


@app.route(
    "/api/receipts/<entry_id>/compressed/<int:target_mb>/reveal",
    methods=["POST"],
)
def reveal_receipt_compressed_variant(entry_id, target_mb):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    entry = find_receipt(entry_id)
    variant = compressed_variant(entry, target_mb) if entry else None
    return reveal_response(variant["path"] if variant else None)


# ── compress to fit · state and routes ──────────────────────────────────────
def compress_update(entry_id, state, fields):
    """Write to a compression's state only while it is still the current one.

    A second compression of the same receipt replaces the dict, and the thread
    that lost the receipt must not keep reporting into the UI — the same
    ownership rule the client's poller follows. Returns whether it still owns it.
    """
    with compressions_lock:
        if compressions.get(entry_id) is not state:
            return False
        state.update(fields)
        return True


def prune_compressions(now=None):
    """Drop long-finished compressions. Caller holds compressions_lock."""
    now = now if now is not None else time.time()
    expired = [
        key for key, state in compressions.items()
        if state.get("status") in TERMINAL_STATUSES
        and now - (state.get("finished_at") or now) > JOB_TTL_S
    ]
    for key in expired:
        del compressions[key]


def compress_presets(source_size, duration):
    """The caps worth offering: smaller than the file, big enough to encode into.

    `duration` is the source page's metadata and may be missing or wrong, so it
    only hides caps that are obviously impossible; compress_plan does the real
    arithmetic against the probed duration once a cap is actually chosen.
    """
    if not source_size:
        return []
    floor_mb = COMPRESS_MIN_TARGET_MB
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
        floor_mb = compress_floor_mb(duration, True)
    return [
        mb for mb in COMPRESS_PRESETS_MB
        if mb * 1024 * 1024 < source_size and mb >= floor_mb
    ]


def compress_payload(entry_id, entry, state=None):
    """One picture of a receipt's compression: what exists, what is running, and
    which caps the UI should offer. Falls back to the ledger so a compression
    from an earlier session still reports done."""
    src = receipt_preview_path(entry) if entry else None
    source_size = os.path.getsize(src) if src else None
    payload = {
        "id": entry_id,
        "status": "idle",
        "target_mb": None,
        "pass": None,
        "error": None,
        "filename": None,
        "size": None,
        "source_filename": os.path.basename(src) if src else None,
        "source_size": source_size,
        "target_min_mb": COMPRESS_MIN_TARGET_MB,
        "target_max_mb": (
            min(MAX_FILESIZE_MB, (source_size - 1) // (1024 * 1024))
            if source_size else 0
        ),
        "presets": compress_presets(source_size, entry.get("duration") if entry else None),
    }
    variants = compressed_variant_payloads(entry)
    payload["variants"] = variants
    compressed = ledger_artifact_path(entry.get("compressed")) if entry else None
    if compressed:
        payload.update({
            "status": "done",
            "filename": entry.get("compressed_filename") or os.path.basename(compressed),
            "size": entry.get("compressed_size") or os.path.getsize(compressed),
            "target_mb": entry.get("compressed_target_mb"),
        })
    elif variants:
        latest = variants[-1]
        payload.update({
            "status": "done",
            "filename": latest["filename"],
            "size": latest["size"],
            "target_mb": latest["target_mb"],
        })
    if state:
        # Live state wins: it describes the newest action on this receipt, which
        # may be a running encode or a failure after an older success.
        payload.update({
            "status": state["status"],
            "target_mb": state["target_mb"],
            "pass": state["pass"],
            "error": state["error"],
        })
        if state.get("filename"):
            payload["filename"] = state["filename"]
            payload["size"] = state["size"]
    return payload


def run_compress(entry_id, state):
    """Encode one receipt's video down to its cap, then record it on the receipt."""
    work_dir = os.path.join(DOWNLOAD_DIR, ".compress-work", entry_id)
    try:
        with compress_slot:
            if not compress_update(entry_id, state, {"status": "running", "pass": 1}):
                return  # superseded while queued: nothing to report, nothing to encode
            entry = find_receipt(entry_id)
            src = receipt_preview_path(entry) if entry else None
            if not src:
                raise RuntimeError("The source video is no longer on disk")
            # The compressed file belongs beside the original, in the receipt's
            # own package folder — one folder is still the whole extraction.
            folder = receipt_folder_path(entry) or os.path.dirname(src)
            shutil.rmtree(work_dir, ignore_errors=True)  # no stale pass-1 log
            os.makedirs(work_dir, exist_ok=True)
            out, size, *_ = compress_to_target(
                src, work_dir, state["target_mb"],
                on_pass=lambda number: compress_update(entry_id, state, {"pass": number}),
            )
            stem = os.path.splitext(os.path.basename(src))[0]
            existing = compressed_variant(entry, state["target_mb"])
            existing_path = existing["path"] if existing else None
            if existing_path and os.path.dirname(existing_path) == os.path.realpath(folder):
                # One cap owns one path. Re-running 8 MB atomically replaces the
                # 8 MB asset instead of creating "8MB 2" and orphaning the first.
                os.replace(out, existing_path)
                final = existing_path
            else:
                final = finalize_path(
                    out, os.path.join(folder, f"{stem} [{state['target_mb']}MB].mp4")
                )
            filename = os.path.basename(final)
            created_at = time.time()
            variants = [
                {
                    "target_mb": item["target_mb"],
                    "path": item["relative_path"],
                    "filename": item["filename"],
                    "size": item["size"],
                    "created_at": item.get("created_at"),
                }
                for item in receipt_compressed_variants(entry)
                if item["target_mb"] != state["target_mb"]
            ]
            variants.append({
                "target_mb": state["target_mb"],
                "path": _ledger_relative(final),
                "filename": filename,
                "size": size,
                "created_at": created_at,
            })
            variants.sort(key=lambda item: item["target_mb"])
            # Two independent failure domains, so two guards, exactly as in
            # run_download: a folder whose manifest has gone unwritable must not
            # cost the compression its ledger line, and neither failure may stop
            # the encode that already succeeded from reaching a terminal status.
            metadata_errors = []
            try:
                patch_manifest(folder, "compressed", final)
                patch_manifest_compressed_variants(folder, variants, final)
            except Exception as exc:
                app.logger.exception("Compression metadata failed for %s", entry_id)
                metadata_errors.append(str(exc))
            try:
                update_ledger_entry(entry_id, {
                    "compressed": _ledger_relative(final),
                    "compressed_filename": filename,
                    "compressed_size": size,
                    "compressed_target_mb": state["target_mb"],
                    "compressed_at": created_at,
                    "compressed_variants": variants,
                })
            except Exception as exc:
                app.logger.exception("Compression metadata failed for %s", entry_id)
                metadata_errors.append(str(exc))
            if metadata_errors:
                raise RuntimeError('Compressed media saved, but metadata could not be saved: ' + '; '.join(metadata_errors))
            # Announced last, so "done" means the receipt already names the file:
            # a client that reloads History the moment it sees done finds it.
            compress_update(entry_id, state, {
                "status": "done", "pass": None, "filename": filename,
                "size": size, "finished_at": time.time(),
            })
    except Exception as exc:
        compress_update(entry_id, state, {
            "status": "error", "pass": None,
            "error": str(exc).strip().split("\n")[-1] or "Compression failed",
            "finished_at": time.time(),
        })
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route("/api/compress/<entry_id>")
def compress_status(entry_id):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    entry = find_receipt(entry_id)
    if not entry:
        return jsonify({"error": "Receipt not found"}), 404
    with compressions_lock:
        state = compressions.get(entry_id)
        snapshot = dict(state) if state else None
    return jsonify(compress_payload(entry_id, entry, snapshot))


@app.route("/api/compress/<entry_id>", methods=["POST"])
def start_compress(entry_id):
    if not JOB_ID_RE.match(entry_id):
        return jsonify({"error": "Invalid receipt id"}), 400
    data, err = parse_json_body()
    if err:
        return err

    target = data.get("target_mb")
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        return jsonify({"error": "target_mb must be a number of megabytes"}), 400
    if not math.isfinite(target) or not float(target).is_integer():
        return jsonify({"error": "target_mb must be a whole number of megabytes"}), 400
    target = int(target)
    if not COMPRESS_MIN_TARGET_MB <= target <= MAX_FILESIZE_MB:
        return jsonify({
            "error": f"target_mb must be between {COMPRESS_MIN_TARGET_MB} and {MAX_FILESIZE_MB}"
        }), 400

    entry = find_receipt(entry_id)
    if not entry:
        return jsonify({"error": "Receipt not found"}), 404
    src = receipt_preview_path(entry)
    if not src:
        return jsonify({"error": "This receipt has no video to compress"}), 404
    if os.path.getsize(src) <= target * 1024 * 1024:
        return jsonify({"error": f"This video is already under {target}MB"}), 400
    if free_disk_mb() < MIN_FREE_DISK_MB:
        return jsonify({"error": "Insufficient disk space to compress"}), 507

    with compressions_lock:
        prune_compressions()
        running = compressions.get(entry_id)
        if running and running["status"] not in TERMINAL_STATUSES:
            return jsonify({"error": "This receipt is already being compressed"}), 409
        state = {
            "status": "queued", "target_mb": target, "pass": None, "error": None,
            "filename": None, "size": None,
            "started_at": time.time(), "finished_at": None,
        }
        compressions[entry_id] = state

    try:
        thread = threading.Thread(target=run_compress, args=(entry_id, state))
        thread.daemon = True
        thread.start()
    except Exception:
        with compressions_lock:
            if compressions.get(entry_id) is state:
                del compressions[entry_id]
        raise

    return jsonify(compress_payload(entry_id, entry, dict(state)))


register_library(app, store=store, load_entries=load_ledger, path_for=ledger_artifact_path,
                 folder_for=receipt_folder_path, preview_for=receipt_preview_path,
                 variants_for=compressed_variant_payloads, probe=probe_compress_source,
                 lock=manifest_lock)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8900))
    host = "127.0.0.1"  # Workshop edition is always local-only.
    app.run(host=host, port=port)
