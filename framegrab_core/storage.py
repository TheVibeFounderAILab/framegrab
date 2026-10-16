"""Durable jobs and a rebuildable library index.

Copyright (c) 2026 TheVibeFounder. Media and manifests remain on disk.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS receipt_search USING fts5(
                    id UNINDEXED, title, creator, url, transcript
                );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def save_job(self, job_id, job):
        payload = json.dumps(job, allow_nan=False)
        with self.lock, self.connect() as db:
            db.execute('''INSERT INTO jobs VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload''',
                (job_id, job.get('request_key'), payload))

    def get_request(self, key):
        if not key:
            return None
        with self.connect() as db:
            row = db.execute('SELECT id, payload FROM jobs WHERE request_key=?', (key,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def get_job(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recover(self):
        """Never silently re-submit work after a process interruption."""
        with self.connect() as db:
            rows = db.execute('SELECT id, payload FROM jobs').fetchall()
        jobs = {}
        for job_id, raw in rows:
            job = json.loads(raw)
            if job.get('status') not in ('done', 'error'):
                job.update(status='error', error_kind='interrupted',
                           error='Service stopped during this job. Resume to continue.',
                           finished_at=time.time(), cancel_requested=False)
                self.save_job(job_id, job)
            jobs[job_id] = job
        return jobs

    def sync_library(self, entries, resolve):
        """Index changed transcripts only. Deleted receipts leave the index too."""
        with self.lock, self.connect() as db:
            old = dict(db.execute('SELECT id, fingerprint FROM receipts'))
            seen = set()
            for entry in entries:
                entry_id = entry['id']
                seen.add(entry_id)
                script = resolve(entry.get('script'))
                stamp = (os.stat(script).st_mtime_ns, os.path.getsize(script)) if script else None
                fingerprint = json.dumps([entry.get('title'), entry.get('uploader'), entry.get('url'), script, stamp])
                if old.get(entry_id) == fingerprint:
                    continue
                transcript = ''
                if script:
                    transcript = Path(script).read_text(encoding='utf-8', errors='replace')
                db.execute('DELETE FROM receipt_search WHERE id=?', (entry_id,))
                db.execute('INSERT INTO receipt_search VALUES (?, ?, ?, ?, ?)',
                           (entry_id, entry.get('title', ''), entry.get('uploader', ''), entry.get('url', ''), transcript))
                db.execute('INSERT OR REPLACE INTO receipts VALUES (?, ?)', (entry_id, fingerprint))
            for entry_id in old.keys() - seen:
                db.execute('DELETE FROM receipt_search WHERE id=?', (entry_id,))
                db.execute('DELETE FROM receipts WHERE id=?', (entry_id,))

    def search(self, query):
        # Treat all user terms as literals, never FTS operators or SQL.
        words = query.split()[:32]
        expression = ' AND '.join('"' + word.replace('"', '""') + '"' for word in words)
        if not expression:
            return []
        with self.connect() as db:
            return [dict(id=row[0], excerpt=row[1]) for row in db.execute(
                "SELECT id, snippet(receipt_search, 4, '', '', ' … ', 24) FROM receipt_search WHERE receipt_search MATCH ? ORDER BY rank",
                (expression,))]
