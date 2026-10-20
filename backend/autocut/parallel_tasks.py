"""Four product workers with process-wide resource and paid-audio limits."""
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path

PRODUCT_POLICY = 'PRODUCT_PARALLEL_V2'
MAX_PRODUCT_JOBS = 4
RESOURCE_LIMITS = dict(asr=2, tts=3, network=2, cpu=2, mps=1, render=2)


class ResourceGate:
    def __init__(self, limit):
        self.limit = limit
        self.active = self.peak = 0
        self.waiters = deque()
        self.condition = threading.Condition()
        self.local = threading.local()

    @contextmanager
    def enter(self):
        # A direct renderer inside a scheduled render uses the same permit.
        if getattr(self.local, 'entered', False):
            yield
            return
        ticket = object()
        with self.condition:
            self.waiters.append(ticket)
            try:
                self.condition.wait_for(lambda: self.waiters[0] is ticket and self.active < self.limit)
            except BaseException:
                self.waiters.remove(ticket)
                self.condition.notify_all()
                raise
            self.waiters.popleft()
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.local.entered = True
            self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.local.entered = False
                self.active -= 1
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(limit=self.limit, active=self.active, waiting=len(self.waiters), peak=self.peak)


_GATES = {name: ResourceGate(limit) for name, limit in RESOURCE_LIMITS.items()}


def resource_slot(resource):
    return _GATES[resource].enter()


def tts_rate_limited():
    gate = _GATES['tts']
    with gate.condition:
        gate.limit = 2
        gate.condition.notify_all()


def resource_status():
    return {name: gate.snapshot() for name, gate in _GATES.items()}


def shared_tts(harness, key, scope, execute, candidates, generate):
    """Serialize identical account/payload requests and publish before release.

    A paid attempt without a complete receipt is never retried by another job.
    The existing engine still checks audio hashes, alignment and approval.
    """
    from app.s3.headless_runtime import locked, read, write
    work = Path(harness.work).resolve()
    if work.parent.parent.name != 'jobs':
        return generate()  # Standalone engine work has its own existing ledger.
    directory = work.parents[2] / 'cache/tts'
    receipt = directory / (key + '.result.json')
    attempt = directory / (key + '.singleflight.json')
    with locked(directory / (key + '.singleflight.lock')):
        try:
            prior = read(attempt, {})
            cached = read(receipt, {})
        except (OSError, ValueError):
            return dict(status='REVIEW_REQUIRED', error='EXISTING_TTS_ARTIFACT_INVALID')
        if cached:
            if cached.get('fingerprint') != key or cached.get('scope') != scope:
                return dict(status='REVIEW_REQUIRED', error='EXISTING_TTS_ARTIFACT_INVALID')
            candidates.insert(0, cached)
        elif prior.get('state') == 'DONE' and prior.get('result'):
            candidates.insert(0, prior['result'])
        elif prior.get('state') in {'IN_FLIGHT', 'REVIEW_REQUIRED'}:
            return dict(status='REVIEW_REQUIRED', error='EXISTING_TTS_ATTEMPT_REQUIRES_RECONCILIATION')
        paid = execute and not candidates
        if paid:
            write(attempt, dict(state='IN_FLIGHT', fingerprint=key, scope=scope, work=str(work)))
        result = generate()
        if result.get('path') and result.get('fingerprint') == key and result.get('scope') == scope:
            write(receipt, result)
            write(attempt, dict(state='DONE', fingerprint=key, scope=scope, result=result))
        elif paid:
            write(attempt, dict(state='REVIEW_REQUIRED', fingerprint=key, scope=scope,
                                error=result.get('error', 'TTS_REVIEW_REQUIRED')))
        return result
