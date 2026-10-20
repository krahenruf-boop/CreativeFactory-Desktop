"""Return only an unrendered, overlong title to its real owner's barrier."""
import copy
from pathlib import Path
from .item_review import get_item, audit
from .production_policy import approved_content
from .ownership import digest
from app.s3.headless_runtime import sha

VERSION = 'TITLE_CONFIRMATION_RECOVERY_V1'


def validate_title(text):
    from app.s3.visual_preset import build_visual_plan
    from .title_layout import apply
    visual = build_visual_plan(5, text, [])
    apply(visual, text, max_lines=2)


def request(manager, ident, rank):
    store = manager.store
    rank = f'{int(rank):02}'
    with store.lock, manager.lock:
        job, item = get_item(store, ident, rank)
        if item.get('title_confirmation_recovery') == VERSION and item['status'] == 'WAITING_TITLE_APPROVAL':
            return
        if (job.get('deleted_at') or job.get('control_request')
                or job.get('state') not in {'PARTIAL_COMPLETED', 'REVIEW_REQUIRED'}
                or (job.get('batch_review') or {}).get('phase') != 'RUNNING'
                or (ident, rank) in manager.item_pending
                or item['status'] != 'FAILED' or item.get('issues') != ['TITLE_SHORTEN_REQUIRED']
                or not approved_content(item) or item.get('plan') or item.get('output')):
            raise ValueError('STATE_TRANSITION_REJECTED')
        audio = item.get('audio_result') or {}
        if not Path(audio.get('path', '')).is_file() or sha(audio['path']) != audio.get('sha256'):
            raise ValueError('RECOVERY_CACHE_INVALID')
        record = dict(previous_title=copy.deepcopy(item.get('final_title')),
                      previous_batch=copy.deepcopy(job['batch_review']),
                      audio_sha256=audio['sha256'], audio_fingerprint=audio.get('fingerprint'))
        store.done(ident, VERSION, rank, digest(record), record)
        meta = copy.deepcopy(job['batch_review'])
        meta.update(phase='TITLES', ranks=[rank])
        store.item(ident, rank, status='WAITING_TITLE_APPROVAL', title_status='WAITING_APPROVAL',
                   content_confirmed=False, title_confirmation_recovery=VERSION,
                   batch_resume_phase='TITLE', resume_pending=False, resume_running=False)
        store.update(ident, state='WAITING_TITLE_APPROVAL', stage='文案', batch_review=meta,
                     title_confirmation_pending=True, script_confirmation_pending=False,
                     scripts_approved=True, titles_approved=False,
                     batch_confirmation_status='TITLES', current_owner='USER')
        audit(store, ident, rank, VERSION, audio_reused=True, script_preserved=True,
              title_not_rewritten=True)
