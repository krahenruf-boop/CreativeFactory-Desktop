"""Headless scheduling/checkpoints. No UI, provider credentials or model imports.

Callbacks do one deterministic stage; visual/content approval remains explicit.
Input fingerprints must include *that stage's* inputs, not unrelated layout.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
STAGES = ('SCAN FFPROBE HASH ASR VIDEO_ANALYSIS DEEPSEEK TITLE_REVIEW TTS ALIGNMENT '
          'OVERLAY_ANALYSIS LIGHT_PREVIEW PROPAINTER_PREVIEW PROPAINTER_FINAL EDIT_PLAN '
          'LAYOUT_PREFLIGHT FINAL_RENDER TECH_QA CONTACT_SHEET VISUAL_QA AUTO_REWORK').split()
CHECKPOINTS = dict(ASR='ASR_DONE', DEEPSEEK='DEEPSEEK_DONE', TITLE_REVIEW='TITLE_APPROVED',
                   TTS='TTS_DONE', OVERLAY_ANALYSIS='OVERLAY_LOCKED',
                   FINAL_RENDER='FINAL_RENDERED', TECH_QA='QA_DONE')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def write(path, value):
    """Atomic, secret-rejecting durable JSON; no raw exception/provider logs."""
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if re.search(r'\bsk[-_][A-Za-z0-9_-]{12,}', text):
        raise ValueError('SECRET_IN_ARTIFACT')
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def union_seconds(events):
    intervals = sorted((e['started_at'], e['ended_at']) for e in events)
    total = 0.; end = 0.
    for a, b in intervals:
        total += max(0., b - max(a, end)); end = max(end, b)
    return round(total, 6)


class Profiler:
    def __init__(self, work):
        self.work = Path(work); self.path = self.work / 'performance.json'

    @contextlib.contextmanager
    def span(self, stage, item='', cache_hit=False, retry_count=0):
        if stage not in STAGES:
            raise ValueError('UNKNOWN_PERFORMANCE_STAGE')
        start, wall, cpu = time.time(), time.monotonic(), time.thread_time()
        e = dict(stage=stage, item=str(item), cache_hit=int(cache_hit), retry_count=retry_count)
        try:
            yield e
        except BaseException:
            e['failed'] = True
            raise
        finally:
            e.update(started_at=start, ended_at=start + time.monotonic() - wall,
                     cpu_seconds=round(time.thread_time() - cpu, 6))
            self.record(e)

    def record(self, event):
        with locked(self.work / '.performance.lock'):
            data = read(self.path, dict(version=1, events=[]))
            data['events'].append(event)
            summary = {}
            for s in STAGES:
                es = [e for e in data['events'] if e['stage'] == s]
                summary[s] = dict(wall_seconds=union_seconds(es),
                    worker_seconds=round(sum(e['ended_at']-e['started_at'] for e in es), 6),
                    cpu_seconds=round(sum(e.get('cpu_seconds', 0) for e in es), 6), count=len(es),
                    cache_hit=sum(e.get('cache_hit', 0) for e in es),
                    retry_count=sum(e.get('retry_count', 0) for e in es),
                    state='RECORDED' if es else 'NOT_OBSERVED')
            data.update(stages=summary, active_wall_seconds=union_seconds(data['events']),
                        elapsed_span_seconds=round(max(e['ended_at'] for e in data['events'])-min(e['started_at'] for e in data['events']),6),
                        cpu_scope='Python calling thread only; excludes subprocess CPU',
                        wall_scope='Union of observed intervals; stages overlap, do not sum wall_seconds')
            write(self.path, data)

    def report(self, report_path):
        data = read(self.path, {'stages': {}})
        lines = ['<!-- PERFORMANCE_START -->', '## Performance Summary', '',
                 '| Stage | Wall s | Worker s | Count | Cache | Retry |',
                 '|---|---:|---:|---:|---:|---:|']
        for s, v in data['stages'].items():
            lines.append(f'| {s} | {v["wall_seconds"]:.2f} | {v["worker_seconds"]:.2f} | '
                         f'{v["count"]} | {v["cache_hit"]} | {v["retry_count"]} |')
        lines += ['', 'Wall为阶段区间并集，Worker为任务耗时之和；阶段可重叠。未观测阶段不估填。',
                  '<!-- PERFORMANCE_END -->']
        path = Path(report_path); old = path.read_text() if path.exists() else ''
        block = '\n'.join(lines)
        if '<!-- PERFORMANCE_START -->' in old:
            old = re.sub(r'<!-- PERFORMANCE_START -->.*?<!-- PERFORMANCE_END -->',
                         lambda _: block, old, flags=re.S)
        else:
            old += '\n\n' + block + '\n'
        path.write_text(old)


class AdaptiveGate:
    def __init__(self, limit=3):
        self.limit, self.active = limit, 0
        self.condition = threading.Condition()

    @contextlib.contextmanager
    def enter(self):
        with self.condition:
            self.condition.wait_for(lambda: self.active < self.limit)
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1; self.condition.notify_all()

    def rate_limited(self):
        with self.condition:
            self.limit = 2; self.condition.notify_all()


class Harness:
    """Independent resource queues; checkpoints serialize only identical jobs.

    Failed paid stages are never implicitly retried. A killed IN_FLIGHT paid
    stage becomes BILLING_UNCERTAIN on resume. Local stages may resume safely.
    """
    def __init__(self, work, render_workers=None):
        self.work = Path(work); self.profiler = Profiler(work)
        config = read(ROOT / 'presets/headless_execution.json', {})
        n = render_workers or config.get('final_render_workers', 1)
        if n not in (1, 2):
            raise ValueError('RENDER_WORKERS_MAX_2')
        self.tts_gate = AdaptiveGate(read(self.work / 'checkpoints/tts_limit.json', {}).get('limit', 3))
        self._hashes={};self._hash_lock=threading.Lock()
        self.pools = {name: ThreadPoolExecutor(max_workers=size, thread_name_prefix='cf_' + name)
                      for name, size in dict(asr=2, tts=3, network=2, cpu=2, mps=1, render=n).items()}

    def dependency_hash(self,path):
        """Immutable model/binary memo, never used for source end-of-task checks."""
        p=Path(path).resolve();s=p.stat();key=(str(p),s.st_size,s.st_mtime_ns,s.st_ino)
        with self._hash_lock:
            if key not in self._hashes:self._hashes[key]=sha(p)
            return self._hashes[key]

    def execute(self, stage, item, inputs, callback, *, paid=False):
        key = fingerprint([stage, inputs])
        path = self.work / 'checkpoints' / (key + '.json')
        with locked(path.with_suffix('.lock')):
            old = read(path)
            if old and old['state'] == 'DONE':
                result = old['result']
                valid = all(Path(a['path']).is_file() and sha(a['path']) == a['sha256']
                            for a in result.get('artifacts', []))
                if valid:
                    with self.profiler.span(stage, item, cache_hit=True):
                        return result | dict(cache_hit=True)
                if paid:
                    return dict(status='REVIEW_REQUIRED', error='PAID_ARTIFACT_CHANGED')
            elif paid and old and old.get('paid',True):
                return dict(status='REVIEW_REQUIRED', error=old.get('error', 'BILLING_UNCERTAIN'))
            write(path, dict(state='IN_FLIGHT', stage=stage, item=str(item), fingerprint=key, paid=paid))
            with self.profiler.span(stage, item) as metric:
                try:
                    result = callback()
                    if not isinstance(result, dict):
                        raise ValueError('STAGE_RESULT_MUST_BE_DICT')
                    if result.get('prior_cache_hit'): metric['cache_hit']=1
                    if result.get('status') in {'FAIL', 'FAILED', 'REVIEW_REQUIRED', 'BILLING_UNCERTAIN'}:
                        write(path, dict(state='REVIEW_REQUIRED', paid=paid,error=result.get('error', 'STAGE_FAILED')))
                        return result
                    write(path, dict(state='DONE', stage=stage, checkpoint=CHECKPOINTS.get(stage, stage+'_DONE'),
                                     fingerprint=key, result=result))
                    return result | dict(cache_hit=bool(result.get('prior_cache_hit') or result.get('cache_hit')))
                except Exception as exc:
                    code = getattr(exc, 'code', None) or (str(exc) if re.fullmatch(r'[A-Z][A-Z0-9_]{1,99}',str(exc)) else 'STAGE_FAILED')
                    if code in {'RATE_LIMITED', 'DEEPSEEK_RATE_LIMITED'} and stage == 'TTS':
                        self.tts_gate.rate_limited()
                        write(self.work / 'checkpoints/tts_limit.json', dict(limit=2))
                    write(path, dict(state='REVIEW_REQUIRED', paid=paid,error=code))
                    return dict(status='REVIEW_REQUIRED', error=code)

    def submit(self, stage, item, inputs, callback, *, paid=False):
        resource = ('asr' if stage == 'ASR' else 'tts' if stage == 'TTS' else
                    'network' if stage == 'DEEPSEEK' else 'mps' if stage.startswith('PROPAINTER_') else
                    'render' if stage == 'FINAL_RENDER' else 'cpu')
        def job():
            with contextlib.ExitStack() as stack:
                if resource == 'tts':
                    stack.enter_context(self.tts_gate.enter())
                if resource == 'mps':
                    # All work directories/processes share one GPU lock.
                    stack.enter_context(locked(Path(tempfile.gettempdir()) / ('cf-mps-'+fingerprint(str(ROOT))+'.lock')))
                return self.execute(stage, item, inputs, callback, paid=paid)
        return self.pools[resource].submit(job)

    def close(self):
        for pool in self.pools.values():
            pool.shutdown(wait=True)
        self.profiler.report(self.work.parent / 'RUN_REPORT.md')

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def propainter_job(harness, region_id, spec, *, correction_reason=None, backend=None):
    """Budget belongs to source+stable region ID, NOT mask/output path/version.

    A second preview requires the first failed visual review to explicitly find
    a mask edge error. Changed parameters alone never reset the budget.
    """
    from .experimental_propainter import ProPainterBackend, validate_spec
    spec=copy.deepcopy(spec)
    validate_spec(spec)
    if (spec.get('overlay_class') not in {'STATIC_OVER_TEXTURE', 'HARD_STATIC_WATERMARK'}
        or spec.get('simple_background') or spec.get('safe_crop_available')
        or not spec.get('mask_path') or not region_id):
        raise ValueError('PROPAINTER_ROUTE_REJECTED')
    identity = dict(source=sha(spec['input_video']), region_id=region_id)
    key = fingerprint(identity)
    path = harness.work / 'budgets' / (key + '.json')
    preview = spec.get('phase', 'preview') == 'preview'
    if preview and not 2 <= spec['end_time'] - spec['start_time'] <= 5:
        raise ValueError('PREVIEW_MUST_BE_2_TO_5_SECONDS')
    mask_hash = sha(spec['mask_path'])
    review = spec.get('mask_review', {})
    if (review.get('status') != 'PASS' or review.get('mask_sha256') != mask_hash
        or review.get('source_sha256') != identity['source'] or not review.get('subject_safe')
        or not review.get('coverage_complete')):
        raise ValueError('MASK_REVIEW_REQUIRED')
    def call():
        if sha(spec['input_video'])!=identity['source'] or sha(spec['mask_path'])!=mask_hash:
            return dict(status='REVIEW_REQUIRED',error='PROPAINTER_INPUT_CHANGED')
        with locked(path.with_suffix('.lock')):
            budget = read(path, dict(previews=0, finals=0, identity=identity))
            if preview:
                previous = spec.get('previous_visual_review', {})
                if budget['previews'] >= 2 or (budget['previews'] == 1 and
                    (correction_reason != 'MASK_EDGE' or previous.get('status') != 'FAIL'
                     or previous.get('issue') != 'MASK_EDGE'
                     or previous.get('output_sha256') != budget.get('last_output_sha256')
                     or mask_hash == budget.get('last_mask_sha256'))):
                    return dict(status='REVIEW_REQUIRED', error='PROPAINTER_PREVIEW_BUDGET_EXHAUSTED')
                budget['previews'] += 1
            else:
                approval = read(spec.get('approved_preview', ''), {}) if spec.get('approved_preview') else {}
                if (budget['finals'] or not approval.get('visual_review_approved')
                    or not budget.get('last_output_sha256')
                    or approval.get('output_sha256') != budget['last_output_sha256']
                    or mask_hash != budget.get('last_mask_sha256')):
                    return dict(status='REVIEW_REQUIRED', error='APPROVED_PREVIEW_OR_FINAL_BUDGET_REQUIRED')
                budget['finals'] += 1
                if budget.get('preview_real_time_factor'):
                    budget['estimated_final_processing_seconds']=budget['preview_real_time_factor']*(spec['end_time']-spec['start_time'])
            write(path, budget)  # Count before inference, including interruption/OOM.
            result = (backend or ProPainterBackend()).run(spec)
            budget.update(last_output_sha256=result.get('output_sha256'), last_mask_sha256=mask_hash)
            if preview and result.get('real_time_factor'):
                budget['preview_real_time_factor']=result['real_time_factor']
            budget.setdefault('attempts',[]).append(dict(phase='preview' if preview else 'final',
                success=result.get('success'),elapsed_seconds=result.get('elapsed_seconds'),
                processing_seconds=result.get('processing_seconds'),mask_sha256=mask_hash))
            write(path, budget)
            if result.get('success'):
                result['artifacts'] = [dict(path=result['output_path'], sha256=result['output_sha256'])]
            return result
    return harness.submit('PROPAINTER_PREVIEW' if preview else 'PROPAINTER_FINAL', region_id,
                          dict(identity=identity, spec=spec, mask_hash=mask_hash), call)
