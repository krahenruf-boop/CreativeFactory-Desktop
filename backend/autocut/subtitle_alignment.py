"""Keep the renderer's alignment checks stable at its existing 80 ms limit."""
import math
import json
from pathlib import Path

from app.s3.subtitles import scaled_alignment as _engine_scaled_alignment

VERSION = 'ALIGNMENT_BOUNDARY_RECOVERY_V1'


def scaled_alignment(alignment, speed, duration):
    try:
        return _engine_scaled_alignment(alignment, speed, duration)
    except ValueError:
        # A provider end of 18.48 can compare greater than 18.4 + .08 solely
        # because the latter rounds to 18.479999999999997. Retry validation
        # only at that boundary; never repair reversed or missing timestamps.
        ends = alignment.get('character_end_times_seconds', [])
        if (not ends or not math.isfinite(speed) or speed <= 0
                or not math.isfinite(duration) or duration <= 0
                or not all(type(t) in (int, float) and math.isfinite(t) for t in ends)):
            raise
        last = max(ends) / speed
        boundary = duration + .08
        if not (last > boundary and math.isclose(last, boundary, rel_tol=0, abs_tol=1e-10)):
            raise
        result = _engine_scaled_alignment(alignment, speed, duration + 1e-10)
        # The exported subtitle window still ends at the actual audio duration.
        for key in ('character_start_times_seconds', 'character_end_times_seconds'):
            result[key] = [min(duration, t) for t in result[key]]
        return result


def request(manager, ident, rank):
    """Resume only the proven boundary failure, retaining the paid audio."""
    from .item_review import get_item, audit
    from .ownership import checkpoints
    from .production_policy import approved_content
    from .script_language import script, target_language, subtitle_guard
    from app.s3.headless_runtime import sha, fingerprint, read

    store = manager.store
    rank = f'{int(rank):02}'
    with store.lock, manager.lock:
        job, item = get_item(store, ident, rank)
        if (ident, rank) in manager.item_pending or item['status'] == 'PASS':
            return
        if (job.get('deleted_at') or job.get('control_request') or manager.control.busy(ident)
                or (job.get('batch_review') or {}).get('phase') != 'RUNNING'
                or item['status'] != 'FAILED' or item.get('issues') != ['JOB_REVIEW_REQUIRED']):
            raise ValueError('STATE_TRANSITION_REJECTED')
        if not approved_content(item):
            raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
        report = store.home / 'jobs' / ident / 'work/RUN_REPORT.jsonl'
        errors = [json.loads(line) for line in report.read_text().splitlines()]
        errors = [row for row in errors if str(row.get('rank')).zfill(2) == rank
                  and row.get('event') == 'ITEM_RECOVERY_ERROR']
        if not errors or errors[-1].get('error') != 'ALIGNMENT_NOT_READY：时间不单调或越界':
            raise ValueError('STATE_TRANSITION_REJECTED')
        audio = item.get('audio_result') or {}
        source = item.get('source_snapshot') or {}
        if (not item.get('analysis') or not item.get('decision')
                or not item.get('copy', {}).get('titles') or item.get('plan') or item.get('output')):
            raise ValueError('RECOVERY_CACHE_INVALID')
        for cached in (audio, source):
            path = Path(cached.get('path', ''))
            if not path.is_file() or sha(path) != cached.get('sha256'):
                raise ValueError('RECOVERY_CACHE_INVALID')
        cp = checkpoints(store, ident, rank)
        receipt = cp.get('TTS_DONE', {})
        if (receipt.get('input_sha') != audio.get('fingerprint')
                or receipt.get('data', {}).get('sha256') != audio.get('sha256')):
            raise ValueError('RECOVERY_CACHE_INVALID')
        raw = read(audio['alignment_path'])
        alignment = raw.get('alignment') or raw.get('normalized_alignment')
        subtitle_guard(script(item), target_language(job), alignment, audio)
        try:
            _engine_scaled_alignment(alignment, 1, audio['duration'])
        except ValueError:
            scaled_alignment(alignment, 1, audio['duration'])
        else:
            raise ValueError('STATE_TRANSITION_REJECTED')
        key = fingerprint([audio['sha256'], sha(audio['alignment_path']), source['sha256']])
        if cp.get(VERSION, {}).get('input_sha') == key:
            raise ValueError('STATE_TRANSITION_REJECTED')
        store.done(ident, VERSION, rank, key, dict(status='REQUESTED',
                   audio_sha256=audio['sha256'], alignment_sha256=sha(audio['alignment_path']),
                   raw_alignment_preserved=True, script_and_title_preserved=True))
        store.item(ident, rank, status='QUEUED', issues=[], resume_pending=True,
                   resume_phase='SCRIPT', alignment_boundary_recovery=VERSION)
        audit(store, ident, rank, VERSION, audio_reused=True,
              script_preserved=True, title_preserved=True, raw_alignment_preserved=True)
        store.update(ident, state='RUNNING', stage='剪辑')
        manager.enqueue_batch(ident, [(rank, 'SCRIPT')])
