"""Library search, asset inspection and bounded frame capture HTTP routes.

Copyright (c) 2026 TheVibeFounder.
"""
import json
import math
import os
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from flask import jsonify, request, send_file
from .artifacts import read_manifest, package_artifact, segments


def canonical_source(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower().removeprefix('www.')
    host = 'x.com' if host == 'twitter.com' else host
    tracking = {'fbclid', 'igsh', 'igshid'}
    if host in ('youtube.com', 'youtu.be'):
        tracking.add('si')
    query = sorted((key, value) for key, value in urllib.parse.parse_qsl(parsed.query)
                   if not key.startswith('utm_') and key not in tracking)
    return host, parsed.port, parsed.path.rstrip('/'), query


def register(app, *, store, load_entries, path_for, folder_for, preview_for, variants_for, probe, lock):
    def receipt(entry_id):
        return next((entry for entry in load_entries() if entry['id'] == entry_id), None)

    def sources(entry):
        folder = folder_for(entry)
        if not folder:
            return []
        try:
            manifest = read_manifest(folder)
            records = manifest.get('artifacts', {}).get('sources', [])
            paths = [package_artifact(folder, record) for record in records]
            paths = [path for path in paths if path]
        except (OSError, ValueError):
            paths = []
        if not paths:
            path = preview_for(entry) or path_for(entry.get('file'))
            if path:
                paths = [path]
        return paths

    @app.get('/api/library')
    def search_library():
        query = request.args.get('q', '').strip()[:1000]
        try:
            offset = max(0, int(request.args.get('offset', 0)))
            limit = min(200, max(1, int(request.args.get('limit', 50))))
        except ValueError:
            return jsonify(error='limit and offset must be integers'), 400
        entries = load_entries()
        if request.args.get('url'):
            try:
                source = canonical_source(request.args['url'])
                entries = [entry for entry in entries if canonical_source(entry.get('url') or '') == source
                           and (request.args.get('format', 'auto') == 'auto' or entry.get('detected_format') == request.args['format'])]
            except ValueError:
                return jsonify(error='Invalid source URL'), 400
            return jsonify(results=[dict(id=e['id'], title=e.get('title'), creator=e.get('uploader')) for e in entries], total=len(entries))
        store.sync_library(entries, path_for)
        hits = store.search(query) if query else [{'id': e['id'], 'excerpt': ''} for e in entries]
        by_id = {entry['id']: entry for entry in entries}
        filtered = []
        for hit in hits:
            entry = by_id.get(hit['id'])
            if not entry:
                continue
            if request.args.get('creator') and entry.get('uploader') != request.args['creator']:
                continue
            if request.args.get('type') and entry.get('detected_format') != request.args['type']:
                continue
            host = urllib.parse.urlsplit(entry.get('url') or '').hostname or ''
            if request.args.get('source') and host.removeprefix('www.') != request.args['source']:
                continue
            filtered.append({**hit, 'title': entry.get('title'), 'creator': entry.get('uploader')})
        return jsonify(results=filtered[offset:offset + limit], total=len(filtered), offset=offset, limit=limit)

    @app.get('/api/receipts/<entry_id>/detail')
    def asset_detail(entry_id):
        entry = receipt(entry_id)
        if not entry:
            return jsonify(error='Receipt not found'), 404
        folder = folder_for(entry)
        transcript_path = path_for(entry.get('transcript'))
        script_path = path_for(entry.get('script'))
        timed = Path(transcript_path).read_text(errors='replace') if transcript_path else ''
        script = Path(script_path).read_text(errors='replace') if script_path else ''
        assets = []
        for index, path in enumerate(sources(entry), 1):
            suffix = Path(path).suffix.lower()
            kind = 'video' if suffix in ('.mp4', '.webm', '.mov', '.m4v') else 'audio' if suffix in ('.mp3', '.m4a', '.wav', '.aac', '.flac', '.ogg') else 'image' if suffix in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif') else 'file'
            assets.append(dict(index=index, filename=Path(path).name, kind=kind,
                               size=os.path.getsize(path), url=f'/api/receipts/{entry_id}/assets/{index}'))
        frames = []
        if folder:
            root = Path(folder).resolve()
            frames_dir = root / 'Frames'
            if frames_dir.is_dir() and root in frames_dir.resolve().parents:
                for path in sorted(frames_dir.iterdir()):
                    if path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') and root in path.resolve().parents and path.is_file():
                        frames.append(dict(filename=path.name, url=f'/api/receipts/{entry_id}/frames/{urllib.parse.quote(path.name)}'))
        return jsonify(id=entry_id, title=entry.get('title'), creator=entry.get('uploader'),
                       source_url=entry.get('url'), folder=folder, assets=assets,
                       script=script, segments=segments(timed), frames=frames,
                       variants=variants_for(entry), can_capture=bool(preview_for(entry)))

    @app.get('/api/receipts/<entry_id>/assets/<int:index>')
    def source_asset(entry_id, index):
        entry = receipt(entry_id)
        assets = sources(entry) if entry else []
        if not 1 <= index <= len(assets):
            return jsonify(error='Source asset not found'), 404
        return send_file(assets[index - 1], conditional=True)

    @app.get('/api/receipts/<entry_id>/frames/<name>')
    def saved_frame(entry_id, name):
        entry = receipt(entry_id)
        folder = folder_for(entry) if entry else None
        if not folder:
            return jsonify(error='Receipt not found'), 404
        root = Path(folder).resolve()
        path = (root / 'Frames' / name).resolve()
        if root not in path.parents or path.parent != (root / 'Frames') or path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp') or not path.is_file():
            return jsonify(error='Frame not found'), 404
        return send_file(path, conditional=True)

    @app.post('/api/frames/<entry_id>')
    def capture_frame(entry_id):
        data = request.get_json(silent=True)
        seconds = data.get('time') if isinstance(data, dict) else None
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            return jsonify(error='time must be a finite, non-negative number of seconds'), 400
        entry = receipt(entry_id)
        source = preview_for(entry) if entry else None
        folder = folder_for(entry) if entry else None
        if not source or not folder:
            return jsonify(error='A saved video receipt is required'), 404
        try:
            duration, _, _ = probe(source)
            if seconds >= duration:
                return jsonify(error='Choose a time before the end of the video'), 400
            with lock:
                root = Path(folder).resolve()
                frames = root / 'Frames'
                if root not in frames.resolve().parents:
                    return jsonify(error='Invalid frames folder'), 400
                frames.mkdir(exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix='.capture-', suffix='.jpg', dir=frames)
                os.close(fd)
                try:
                    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(seconds), '-i', source,
                                    '-frames:v', '1', '-q:v', '2', tmp], check=True, capture_output=True, timeout=30)
                    if not os.path.getsize(tmp):
                        raise ValueError('No frame was produced at this time')
                    name = f'frame-{round(seconds * 1000):010d}.jpg'
                    path = frames / name
                    os.replace(tmp, path)
                    manifest = read_manifest(folder)
                    inventory = manifest.setdefault('artifacts', {}).setdefault('frames', [])
                    inventory[:] = [item for item in inventory if item.get('filename') != name]
                    inventory.append(dict(filename=name, time=seconds, absolute_path=str(path),
                                          relative_path=str(path.relative_to(root.parent))))
                    fd, manifest_tmp = tempfile.mkstemp(prefix='.manifest-', dir=root)
                    try:
                        with os.fdopen(fd, 'w') as handle:
                            json.dump(manifest, handle, indent=2)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(manifest_tmp, root / 'manifest.json')
                    finally:
                        if os.path.exists(manifest_tmp):
                            os.unlink(manifest_tmp)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            return jsonify(filename=name, path=str(path), time=seconds, url=f'/api/receipts/{entry_id}/frames/{name}')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            app.logger.exception('Frame capture failed for %s', entry_id)
            return jsonify(error=f'Frame capture could not be completed: {exc}'), 500
