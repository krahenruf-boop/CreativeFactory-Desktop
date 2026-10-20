"""Private integration journal. No migration of the Creative Factory database."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Journal:
    def __init__(self, home):
        self.root = Path(home) / 'integrations/feishu_tasks'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'journal.sqlite3'
        if self.path.is_symlink():
            raise ValueError('FEISHU_TASK_PATH_UNSAFE')
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY,v TEXT);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,body TEXT,state TEXT,created REAL,error TEXT);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,data TEXT);
                CREATE TABLE IF NOT EXISTS forms(id TEXT PRIMARY KEY,data TEXT);
                CREATE TABLE IF NOT EXISTS outbox(k TEXT PRIMARY KEY,data TEXT,state TEXT,attempts INTEGER,next REAL,error TEXT);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,data TEXT);
            ''')
            # Only integration work is recovered here, never any historical jobs.
            db.execute("UPDATE events SET state='PENDING' WHERE state='WORKING'")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self):
        with self.connect() as db:
            values = {r['k']: json.loads(r['v']) for r in db.execute('SELECT * FROM config')}
        return dict({'enabled': False, 'accepting': False, 'chat_id': '', 'admin_open_id': ''}, **values)

    def operation(self, event_id, payload=None):
        with self.lock, self.connect() as db:
            if payload is not None:
                db.execute('INSERT OR IGNORE INTO operations VALUES(?,?)', (event_id, json.dumps(payload, ensure_ascii=False)))
            row = db.execute('SELECT data FROM operations WHERE id=?', (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def configure(self, **values):
        with self.lock, self.connect() as db:
            for key, value in values.items():
                db.execute('INSERT OR REPLACE INTO config VALUES(?,?)', (key, json.dumps(value)))

    def bind_code(self):
        code = secrets.token_urlsafe(9)
        self.configure(bind_hash=fingerprint(code), bind_until=time.time() + 600)
        return code

    def bind(self, chat, operator, code):
        with self.lock:
            c = self.config()
            if c.get('chat_id') or c.get('bind_until', 0) < time.time() or not secrets.compare_digest(c.get('bind_hash', ''), fingerprint(code)):
                raise ValueError('FEISHU_TASK_BIND_INVALID')
            self.configure(chat_id=chat, admin_open_id=operator, bind_hash='', bind_until=0)

    def record_event(self, event_id, body):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT body FROM events WHERE id=?', (event_id,)).fetchone()
            if row:
                if fingerprint(json.loads(row['body'])) != fingerprint(body):
                    raise ValueError('STALE_ACTION')
                return False
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (event_id, json.dumps(body, ensure_ascii=False), 'PENDING', time.time(), ''))
            return True

    def next_event(self):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM events WHERE state='PENDING' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE events SET state='WORKING' WHERE id=?", (row['id'],))
            return row['id'], json.loads(row['body'])

    def event_done(self, event_id, error=''):
        with self.connect() as db:
            db.execute('UPDATE events SET state=?,error=? WHERE id=?', ('ERROR' if error else 'DONE', error, event_id))

    def form(self, owner, chat, kind='new', task_id='', defaults=None):
        form_id = uuid.uuid4().hex
        data = dict(owner=owner, chat=chat, kind=kind, task_id=task_id, expires=time.time() + 86400, defaults=defaults or {}, consumed=False)
        with self.connect() as db:
            db.execute('INSERT INTO forms VALUES(?,?)', (form_id, json.dumps(data, ensure_ascii=False)))
        return form_id

    def get_form(self, form_id, operator, chat):
        with self.connect() as db:
            row = db.execute('SELECT data FROM forms WHERE id=?', (form_id,)).fetchone()
        if not row:
            raise ValueError('STALE_ACTION')
        data = json.loads(row[0])
        if data['chat'] != chat or data['owner'] != operator:
            raise ValueError('FEISHU_TASK_FORBIDDEN')
        if data['expires'] < time.time():
            raise ValueError('STALE_ACTION')
        return data

    def update_form(self, form_id, **values):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT data FROM forms WHERE id=?', (form_id,)).fetchone()
            if not row:
                raise ValueError('STALE_ACTION')
            data = dict(json.loads(row[0]), **values)
            db.execute('UPDATE forms SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), form_id))
        return data

    def submitted_task(self, form_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM tasks WHERE request_id=?', ('feishu-' + form_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def create_task(self, form_id, data):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id FROM tasks WHERE request_id=?', ('feishu-' + form_id,)).fetchone()
            if row:
                existing = self.task(row[0])
                if existing.get('submission_digest') != fingerprint(data):
                    raise ValueError('STALE_ACTION')
                return existing, False
            task_id = 'CF-' + time.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:8].upper()
            data = dict(data, submission_digest=fingerprint(data), id=task_id, request_id='feishu-' + form_id, state='RECEIVED', job_id='', page=0,
                        error='', selections={}, selection_revision='', received_at=time.time(), delivery={}, revision=0)
            db.execute('INSERT INTO tasks VALUES(?,?,?)', (task_id, data['request_id'], json.dumps(data, ensure_ascii=False)))
            row = json.loads(db.execute('SELECT data FROM forms WHERE id=?', (form_id,)).fetchone()[0])
            row['consumed'] = True
            db.execute('UPDATE forms SET data=? WHERE id=?', (json.dumps(row, ensure_ascii=False), form_id))
        return data, True

    def task(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise ValueError('FEISHU_TASK_NOT_FOUND')
        return json.loads(row[0])

    def tasks(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM tasks ORDER BY rowid')]

    def update(self, task_id, **values):
        with self.lock, self.connect() as db:
            data = self.task(task_id)
            data.update(values)
            db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), task_id))
        return data

    def notify(self, key, chat, card, task_id='', message_id='', mention=''):
        data = dict(chat=chat, card=card, task_id=task_id, message_id=message_id, mention=mention)
        with self.lock, self.connect() as db:
            db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,?)', (key, json.dumps(data, ensure_ascii=False), 'PENDING', 0, 0, ''))

    def pending_notifications(self):
        with self.connect() as db:
            return [dict(r, data=json.loads(r['data'])) for r in db.execute("SELECT * FROM outbox WHERE state='PENDING' AND next<=? ORDER BY rowid LIMIT 10", (time.time(),))]

    def delivered(self, key):
        with self.connect() as db:
            db.execute("UPDATE outbox SET state='DONE',error='' WHERE k=?", (key,))

    def suppress(self, key, reason):
        """Cancel a pending notification without recording a fictional send."""
        with self.connect() as db:
            db.execute("UPDATE outbox SET state='SKIPPED',error=? WHERE k=? AND state='PENDING'", (reason, key))

    def defer(self, key, attempts, error):
        with self.connect() as db:
            db.execute('UPDATE outbox SET attempts=?,next=?,error=? WHERE k=?', (attempts, time.time() + min(300, 5 * 2 ** min(attempts, 6)), error, key))
