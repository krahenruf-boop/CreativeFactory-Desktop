"""Explicit, source-hash-bound height-fit exception; paid audio is immutable."""
import copy
from .item_review import get_item, audit
from .production_policy import approved_content
from .source_frame import POLICY, AUTO_POLICY, HEIGHT_POLICY, geometry
from .task_instructions import digest
from app.s3.headless_runtime import sha

VERSION = 'SOURCE_FRAME_HEIGHT_FIT_RECOVERY_V1'


def request(manager, ident, source_rank, ranks):
    store = manager.store
    with store.lock, manager.lock:
        job, source_item = get_item(store, ident, source_rank)
        if (job.get('deleted_at') or job.get('control_request')
                or job.get('state') in {'COMPLETED', 'PAUSING', 'PAUSED', 'STOPPING', 'STOPPED'}
                or manager.control.busy(ident) or job.get('source_frame_mode') not in {POLICY, AUTO_POLICY}
                or (job.get('batch_review') or {}).get('phase') != 'RUNNING'):
            raise ValueError('STATE_TRANSITION_REJECTED')
        if not isinstance(ranks, list) or not ranks or len(ranks) > 30:
            raise ValueError('INVALID_REQUEST')
        ranks = [f'{int(rank):02}' for rank in ranks]
        if len(set(ranks)) != len(ranks):raise ValueError('INVALID_REQUEST')
        source = source_item.get('source_snapshot') or {}
        if not source_item.get('analysis') or sha(source['path']) != source['sha256']:
            raise ValueError('SOURCE_HASH_CHANGED')
        layout = geometry(source, 1080, 1920, HEIGHT_POLICY)
        if not layout or layout['policy'] != HEIGHT_POLICY:
            raise ValueError('STATE_TRANSITION_REJECTED')
        key = digest([VERSION, source['sha256'], ranks])
        if store.checkpoint(ident, VERSION, 'all', key):return
        rows = []
        pre_tts = []
        for rank in ranks:
            _, item = get_item(store, ident, rank)
            if item['status'] == 'PASS':continue
            if (item['status'] not in {'FAILED', 'REVIEW_REQUIRED'}
                    or item.get('issues') != ['SOURCE_FRAME_TOO_TALL']
                    or not approved_content(item) or item.get('output')):
                raise ValueError('STATE_TRANSITION_REJECTED')
            audio = item.get('audio_result') or {}
            if audio:
                if (not audio.get('path') or sha(audio['path']) != audio.get('sha256')
                        or audio.get('script_text') != item.get('final_script')):
                    raise ValueError('RECOVERY_CACHE_INVALID')
            else:
                # New jobs can stop at the frame guard before their first TTS.
                # Missing paid audio is never treated as permission to replace it.
                from .source_library import POLICY as PREFLIGHT_POLICY
                with store.connect() as db:
                    paid = db.execute("SELECT 1 FROM checkpoints WHERE job_id=? AND stage='TTS_DONE' AND item=?",
                                      (ident, rank)).fetchone()
                if (item['status'] != 'REVIEW_REQUIRED'
                        or job.get('production_preflight_policy') != PREFLIGHT_POLICY
                        or item.get('plan') or paid):
                    raise ValueError('RECOVERY_CACHE_INVALID')
                pre_tts.append(rank)
            rows.append((rank, 'SCRIPT'))
        if not rows:return
        exceptions = copy.deepcopy(job.get('source_frame_exceptions') or {})
        exceptions[source['sha256']] = HEIGHT_POLICY
        store.done(ident, VERSION, 'all', key, dict(source_rank=f'{int(source_rank):02}',
            source_sha256=source['sha256'], source_path=source['path'], layout=layout,
            ranks=ranks, paid_audio_preserved=True, foreground_cropped=False,
            pre_tts_ranks=pre_tts))
        store.update(ident, source_frame_exceptions=exceptions, state='RUNNING', stage='剪辑')
        for rank, phase in rows:
            store.item(ident, rank, status='QUEUED', issues=[], resume_pending=True, resume_phase=phase)
            audit(store, ident, rank, VERSION, source_sha256=source['sha256'], original_audio_reused=True)
        manager.enqueue_batch(ident, rows)
