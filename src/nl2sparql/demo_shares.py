"""Opt-in unlisted demo snapshots, created only from server-released results.

Drafts live briefly in bounded process memory. Published snapshots contain the question and
released aggregate table, never queries, diagnostics, credentials, or raw withheld rows.
SQLite is opened only by the demo sharing routes, never by the judged pipeline.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from nl2sparql.demo_results import POLICY_VERSION, ROW_LIMIT

TOKEN = re.compile(r'^[A-Za-z0-9_-]{43}$')
DRAFT_SECONDS = 3600
MAX_DRAFTS = 128
MAX_DRAFT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_SHARES = 1000
MAX_STORED_BYTES = 64 * 1024 * 1024


class ShareError(Exception):
    def __init__(self, message, status=404):
        super().__init__(message)
        self.status = status


def _timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _matches(snapshot, threshold):
    if threshold is not None and (type(threshold) is not int or threshold < 1):
        return False
    if threshold is None:
        return True
    policy = snapshot.get('suppression')
    return (isinstance(policy, dict) and policy.get('enabled') is True
            and policy.get('status') == 'released' and policy.get('threshold') == threshold
            and policy.get('policy_version') == POLICY_VERSION)


class ShareStore:
    def __init__(self, path: Path, ttl_seconds: int, clock=time.time):
        if type(ttl_seconds) is not int or ttl_seconds < 1:
            raise ValueError('DEMO_SHARE_TTL_SECONDS must be a positive integer.')
        self.path, self.ttl_seconds, self.clock = Path(path), ttl_seconds, clock
        self._drafts = OrderedDict()
        self._lock = threading.RLock()

    def _prune_drafts(self):
        now = self.clock()
        for token, draft in list(self._drafts.items()):
            if draft['expires'] <= now:
                del self._drafts[token]
        while len(self._drafts) > MAX_DRAFTS or sum(d['bytes'] for d in self._drafts.values()) > MAX_DRAFT_BYTES:
            self._drafts.popitem(last=False)

    def stage(self, question: str, result: dict) -> str | None:
        """Capture an authoritative result; no disk access and no client-supplied rows."""
        if result.get('result_status') != 'released':
            return None
        columns = result['columns']
        if not columns or len(columns) > 100:
            return None
        snapshot = dict(format_version=1, question=question, columns=columns,
                        rows=[[row.get(c) for c in columns] for row in result['rows']],
                        column_metadata=result['column_metadata'], captured_at=_timestamp(self.clock()),
                        truncated=result['truncated'], row_limit=ROW_LIMIT, suppression=result['suppression'])
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        size = len(payload.encode('utf-8'))
        if size > MAX_SNAPSHOT_BYTES:
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._drafts[token] = dict(payload=payload, bytes=size, expires=self.clock() + DRAFT_SECONDS, published=None)
            self._prune_drafts()
        return token

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute('''CREATE TABLE IF NOT EXISTS snapshots
            (id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL)''')
        return connection

    def publish(self, token: str, threshold: int | None) -> dict:
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ShareError('Ask again before sharing this result.')
        with self._lock:
            self._prune_drafts()
            draft = self._drafts.get(token)
            if draft is None:
                raise ShareError('Ask again before sharing this result.')
            if not _matches(json.loads(draft['payload']), threshold):
                raise ShareError('Privacy policy changed. Re-ask before sharing.', 409)
            now = self.clock()
            if draft['published'] and draft['published']['expires'] > now:
                value = draft['published']
                return dict(share_id=value['id'], expires_at=_timestamp(value['expires']))
            with closing(self._connect()) as connection, connection:
                # Serialize capacity checks across processes sharing this file.
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('DELETE FROM snapshots WHERE expires <= ?', (now,))
                count, size = connection.execute('SELECT COUNT(*), COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM snapshots').fetchone()
                if count >= MAX_SHARES or size + draft['bytes'] > MAX_STORED_BYTES:
                    raise ShareError('Sharing capacity reached. Try again later.', 503)
                share_id, expires = secrets.token_urlsafe(32), now + self.ttl_seconds
                connection.execute('INSERT INTO snapshots VALUES (?, ?, ?)', (share_id, draft['payload'], expires))
            draft['published'] = dict(id=share_id, expires=expires)
            return dict(share_id=share_id, expires_at=_timestamp(expires))

    def get(self, share_id: str, threshold: int | None) -> dict:
        if not TOKEN.fullmatch(share_id):
            raise ShareError('This shared result is unavailable or expired.')
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute('SELECT payload, expires FROM snapshots WHERE id = ?', (share_id,)).fetchone()
            if row is None or row[1] <= self.clock():
                if row:
                    connection.execute('DELETE FROM snapshots WHERE id = ?', (share_id,))
                    connection.commit()
                raise ShareError('This shared result is unavailable or expired.')
            snapshot = json.loads(row[0])
            if not _matches(snapshot, threshold):
                raise ShareError('Privacy policy changed. Re-ask for a current result.', 409)
            return dict(snapshot=snapshot, expires_at=_timestamp(row[1]))
