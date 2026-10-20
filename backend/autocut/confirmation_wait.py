"""Timed human waits; durable metadata, no approval or production-record writes."""
import copy
import hashlib
import json
import os
import threading
import time
import uuid

from .batch_confirmation import batch_scope

VERSION = 'CONFIRMATION_WAIT_10_20_V1'
POLICY = dict(version=VERSION, enabled=True, remind_after_seconds=600,
              suspend_after_seconds=1200, reminder_once_per_wait=True,
              resume_after_active_tasks=True)
HELD = {'COMPLETED', 'PAUSED', 'PAUSING', 'STOPPED', 'STOPPING'}
EXTRA = {'WAITING_SUPPLEMENT_MEDIA': '补充画面',
         'WAITING_FINAL_REVIEW': '成片', 'WAITING_OVERLAY_APPROVAL': '原文字处理'}


class ConfirmationWait:
    def __init__(self, store, manager, bridge, clock=time.time):
        self.store, self.manager, self.bridge, self.clock = store, manager, bridge, clock
        self.path = store.home / 'runtime/confirmation-wait-state.json'
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = ''
        self.dispatching = set()
        # A damaged ledger is an error, never permission to send reminders again.
        self.data = json.loads(self.path.read_text()) if self.path.exists() else dict(
            policy=POLICY, enabled_at=clock(), jobs={})
        self.data['policy'] = dict(POLICY)
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def _eligible(self, job):
        if job.get('deleted_at') or job.get('control_request') or job['state'] in HELD:
            return None
        if self._busy(job['id']) or any(x.get('operation_inflight') for x in job['items']):
            return None
        phase, ranks = batch_scope(job)
        if phase in {'SCRIPTS', 'TITLES'} and job['state'] not in {'RUNNING', 'QUEUED'}:
            return phase, list(ranks)
        for status in EXTRA:
            ranks = [x['rank'] for x in job['items'] if x['status'] == status]
            if ranks:
                return status, ranks
        return None

    def _busy(self, ident):
        control = getattr(self.manager, 'control', None)
        if control:
            return control.busy(ident)
        future = getattr(self.manager, 'active', {}).get(ident)
        return bool(future and not future.done())

    def _blockers(self, ident):
        return [key for key, future in list(getattr(self.manager, 'active', {}).items())
                if key != ident and not future.done()]

    def _confirmed(self, job, row):
        items = {x['rank']: x for x in job['items']}
        scoped = [items.get(rank, {}) for rank in row.get('ranks', [])]
        if not scoped:
            return False
        if row['phase'] == 'SCRIPTS':
            return all(x.get('script_status') == 'USER_APPROVED' for x in scoped)
        if row['phase'] == 'TITLES':
            return all(x.get('content_confirmed') and x.get('title_status') == 'USER_APPROVED' for x in scoped)
        return all(x.get('status') != row['phase'] and
                   (x.get('resume_pending') or x.get('status') in {'QUEUED', 'PASS', 'USER_APPROVED'})
                   for x in scoped)

    def _confirmed_transition(self, job, row):
        if row.get('suspended') and self._confirmed(job, row):
            row['suspended'] = False
            row['confirmed_at'] = self.clock()
            row['resume_after'] = self._blockers(job['id'])
            row['phase'] = 'CONFIRMED'
            return True
        return False

    def confirmed(self, ident):
        """Called only after an existing, validated human confirmation commits."""
        with self.store.lock, self.lock:
            row = self.data['jobs'].get(ident)
            if row and self._confirmed_transition(self.store.get(ident), row):
                self._save()

    def defer(self, ident, spec):
        """Keep queue dependencies outside product workers and resource permits."""
        with self.store.lock, self.lock:
            job = self.store.get(ident)
            if job.get('deleted_at') or job.get('control_request') or job['state'] in HELD:
                return False  # Existing manual-control checks retain authority.
            row = self.data['jobs'].get(ident)
            if not row:
                return False
            changed = self._confirmed_transition(job, row)
            blockers = [key for key in row.get('resume_after', []) if self._busy(key)]
            row['resume_after'] = blockers
            if row.get('suspended') or blockers or row.get('pending') and ident not in self.dispatching:
                pending = row.setdefault('pending', [])
                if spec not in pending:
                    pending.append(copy.deepcopy(spec))
                self._save()
                return True
            if changed:
                self._save()
            return False

    def _prior_reminder(self, ident, phase, since):
        # The explicitly requested one-time reminder also satisfies this wait.
        for path in (self.store.home / 'runtime').glob('manual-review-reminder-*.json'):
            data = json.loads(path.read_text())
            found = next((r.get('finished_at') for r in data.get('targets', [])
                          if r.get('job_id') == ident and r.get('phase') == phase
                          and r.get('finished_at', 0) >= since
                          and r.get('status') in {'MENTION_REMINDER_SENT', 'URGENT_APP_SENT'}), None)
            if found:
                return found
        return None

    def tick(self):
        dispatch = []
        with self.store.lock, self.lock:
            before = json.dumps(self.data, sort_keys=True)
            for job in self.store.jobs():
                ident = job['id']
                row = self.data['jobs'].get(ident)
                if job.get('deleted_at') or job.get('control_request') or job['state'] in HELD:
                    if row:
                        row.update(suspended=False, pending=[], resume_after=[], phase='CLOSED')
                    continue
                if row:
                    self._confirmed_transition(job, row)
                wait = self._eligible(job)
                if wait:
                    phase, ranks = wait
                    if not row or (row['phase'], row.get('ranks')) != (phase, ranks):
                        prior = row or {}
                        # On first adoption, use the existing job's last stage
                        # transition. Future cycles start when observed, not at
                        # an unrelated old item timestamp.
                        since = min(self.clock(), job.get('updated', self.clock())) if not row else self.clock()
                        sequence = int(prior.get('sequence', 0)) + 1
                        cycle = hashlib.sha256((ident + ':' + str(sequence) + ':' + uuid.uuid4().hex).encode()).hexdigest()[:24]
                        row = dict(phase=phase, ranks=ranks, sequence=sequence, cycle=cycle,
                                   waiting_since=since, suspended=False,
                                   reminder_enqueued=bool(self._prior_reminder(ident, phase, since)) if not prior else False,
                                   resume_after=prior.get('resume_after', []), pending=prior.get('pending', []))
                        self.data['jobs'][ident] = row
                    elapsed = self.clock() - row['waiting_since']
                    local_only = not self.bridge.confirmation_task(ident)
                    if local_only:
                        row['reminder_suppressed_reason'] = 'LOCAL_JOB_NO_FEISHU'
                    else:
                        row.pop('reminder_suppressed_reason', None)
                    if not local_only and elapsed >= POLICY['remind_after_seconds'] and not row['reminder_enqueued']:
                        if self.bridge.queue_confirmation_reminder(job, row):
                            row.update(reminder_enqueued=True, reminder_enqueued_at=self.clock())
                    if elapsed >= POLICY['suspend_after_seconds']:
                        row['suspended'] = True
                elif (row and not row.get('pending') and not row.get('resume_after')
                      and (row['phase'] == 'CONFIRMED' or self._confirmed(job, row))):
                    row.update(phase='CLOSED', suspended=False)
                if row and row.get('pending') and not row.get('suspended') and not self._busy(ident):
                    row['resume_after'] = [key for key in row.get('resume_after', []) if self._busy(key)]
                    if not row['resume_after']:
                        # Enqueue while retaining the durable intent. A crash
                        # retries the same request; Manager already deduplicates.
                        dispatch.append((ident, copy.deepcopy(row['pending'])))
            if before != json.dumps(self.data, sort_keys=True):
                self._save()
        for ident, specs in dispatch:
            with self.lock:
                self.dispatching.add(ident)
            try:
                for spec in specs:
                    if spec['kind'] == 'items':
                        self.manager.enqueue_batch(ident, spec['rows'])
                    elif spec['kind'] == 'item':
                        self.manager.enqueue_item(ident, spec['rank'], spec['phase'])
                    elif spec['kind'] == 'titles':
                        self.manager.prepare_batch_titles(ident, spec['ranks'])
                    else:
                        self.manager.enqueue(ident)
                    with self.store.lock, self.lock:
                        pending = self.data['jobs'][ident].get('pending', [])
                        if spec in pending:
                            pending.remove(spec)
                        self._save()
            finally:
                with self.lock:
                    self.dispatching.discard(ident)

    def can_send(self, key):
        with self.store.lock, self.lock:
            for ident, row in self.data['jobs'].items():
                if key == 'reviewwait-' + ident + '-' + row.get('cycle', ''):
                    if not self.bridge.confirmation_task(ident):return False
                    try:
                        wait = self._eligible(self.store.get(ident))
                    except KeyError:
                        return False
                    return bool(wait and (row['phase'], row['ranks']) == wait)
        return False

    def view(self, job):
        with self.lock:
            row = copy.deepcopy(self.data['jobs'].get(job['id']) or {})
        if job.get('deleted_at') or job.get('control_request') or job['state'] in HELD:
            return dict(policy=VERSION, status='INACTIVE', label='')
        name = {'SCRIPTS': '口播', 'TITLES': '标题', **EXTRA}.get(row.get('phase'), '内容')
        label = ('已挂起·待确认' + name) if row.get('suspended') else '已确认·等待前序任务' if row.get('pending') else ''
        return dict(policy=VERSION, status='SUSPENDED' if row.get('suspended') else 'QUEUED_AFTER_ACTIVE' if row.get('pending') else 'WAITING' if row.get('phase') not in {None, 'CLOSED', 'CONFIRMED'} else 'INACTIVE',
                    label=label, waiting_since=row.get('waiting_since'), reminder_enqueued=row.get('reminder_enqueued', False),
                    reminder_suppressed_reason=row.get('reminder_suppressed_reason'),
                    resume_after=row.get('resume_after', []), phase=row.get('phase'))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                    self.last_error = ''
                except Exception:
                    self.last_error = 'CONFIRMATION_WAIT_CHECK_FAILED'
                self.stop_event.wait(2)
        self.thread = threading.Thread(target=loop, daemon=True, name='confirmation-wait')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
