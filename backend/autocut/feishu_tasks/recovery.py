"""Recover only a bridge-owned job, without replaying approved batch analysis."""
from ..batch_confirmation import batch_view, valid_title
from ..production_policy import approved_content
from .. import ai_recovery


def busy(manager, job):
    future = getattr(manager, 'active', {}).get(job['id'])
    return bool(future and not future.done()) or any(
        job_id == job['id'] for job_id, _ in getattr(manager, 'item_pending', set()))


def resume(manager, job_id, task_id, automatic=False):
    store = manager.store
    with store.lock, manager.lock:
        job = store.get(job_id)
        if (job.get('submission_origin') or {}).get('task_id') != task_id:
            raise ValueError('FEISHU_TASK_FORBIDDEN')
        if job.get('deleted_at') or job.get('control_request') or job['state'] in {'PAUSED', 'STOPPED', 'STOPPING', 'PAUSING'}:
            return False
        if busy(manager, job):
            return True
        view = batch_view(store, job)
        if view['phase'] == 'SCRIPTS':
            if view['all_scripts_ready']:
                return False
            # Never overwrite a manual replacement after an interrupted edit.
            if any(x.get('script_draft') or x.get('approved_draft_id') or x.get('content_confirmed') for x in job['items']):
                raise ValueError('FEISHU_TASK_SCRIPT_REVIEW_REQUIRED')
            # Before the first approval barrier, the existing pipeline consumes
            # its exact-source analysis/copy caches. No TTS has been released.
            manager.enqueue(job_id)
            return True
        if view['phase'] == 'TITLES':
            missing = [x['rank'] for x in job['items'] if x['rank'] in view['ranks'] and
                       (len(x.get('title_candidates') or []) != 3 or
                        not all(valid_title(t, job) for t in x['title_candidates']))]
            if missing:
                store.update(job_id, title_prepare_failed=False)
                manager.prepare_batch_titles(job_id, missing)
            return bool(missing)
        rows = []
        for item in job['items']:
            if item['status'] in {'PASS', 'USER_APPROVED'} or not approved_content(item):
                continue
            pending = item.get('resume_pending') or item.get('resume_running') or item['status'] in {
                'QUEUED', 'VOICE', 'EDITING', 'OVERLAY', 'RENDERING', 'CHECKING', 'DURATION_FIT', 'SCRIPTING', 'INTERRUPTED'}
            if not pending and (automatic or item['status'] != 'FAILED'):
                continue
            phase = item.get('resume_phase') or 'TITLE'
            if set(item.get('issues') or []) & ai_recovery.ERRORS or (pending and item.get('output')):
                if item.get('output'):
                    ai_recovery.final_evidence(store, job, item)
                    phase = 'AI_FINAL_QA'
                elif item.get('plan'):
                    ai_recovery.render_evidence(item)
                    phase = 'AI_RENDER_QA'
            rows.append((item['rank'], phase))
        # Validate all reuse evidence before any queue mutation. Completed rows
        # are never passed to the item worker, even after a repeated callback.
        for rank, phase in rows:
            store.item(job_id, rank, status='QUEUED', issues=[], resume_pending=True,
                       resume_running=False, resume_phase=phase)
        if rows:
            store.update(job_id, state='QUEUED', stage='剪辑')
            manager.enqueue_batch(job_id, rows)
        return bool(rows)
