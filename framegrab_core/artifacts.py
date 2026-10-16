"""Safe receipt inspection and timestamp parsing.

Copyright (c) 2026 TheVibeFounder.
"""
import json
import re
from pathlib import Path


def read_manifest(folder):
    path = Path(folder) / 'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported receipt manifest')
    return data


def package_artifact(folder, record):
    """Prefer existing in-package paths; recover paths after moving Downloads."""
    root = Path(folder).resolve()
    if not isinstance(record, dict):
        return None
    candidates = [record.get('absolute_path'), root / str(record.get('filename', ''))]
    for value in candidates:
        if not value:
            continue
        path = Path(value).resolve()
        if root in path.parents and path.is_file():
            return str(path)
    return None


def segments(text):
    result = []
    for line in text.splitlines():
        match = re.match(r'^\[(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\]\s*(.*)$', line)
        if match:
            result.append({'start': float(match[1]), 'end': float(match[2]), 'text': match[3]})
    return result
