"""One local-ASR retry before script approval; never touch paid audio."""
import copy
from .context import ENGINE, RES
from .item_review import get_item, audit
from .ownership import require_action
from .source_analysis import analyze_sources
from .task_instructions import digest
from app.s3.headless_jobs import asr_job
from app.s3.headless_runtime import Harness, read, write, sha, fingerprint

VERSION = 'SOURCE_ASR_DRAFT_RECOVERY_V1'


def recover(review, ident, rank, token):
    store, manager = review.store, review.manager
    rank = f'{int(rank):02}'
    with review.item_lock(ident, rank):
        with store.lock:
            job, item, _ = require_action(store, ident, rank, 'CHANGE_SCRIPT', token)
            if (item.get('issues') != ['NO_RELIABLE_SCRIPT'] or item.get('final_script')
                    or item.get('script_draft') or item.get('audio_result') or item.get('output')
                    or item.get('script_status') == 'USER_APPROVED' or item.get('batch_script_approved')):
                raise ValueError('STATE_TRANSITION_REJECTED')
            source = copy.deepcopy(item['source_snapshot'])
            if sha(source['path']) != source['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
            key = digest([VERSION, source['sha256']])
            if store.checkpoint(ident, VERSION, rank, key):raise ValueError('RECOVERY_ALREADY_REQUESTED')
        work = store.home/'jobs'/ident/'work'
        with Harness(work, render_workers=1) as harness:
            model, binary = ENGINE/'models/ggml-base.bin', RES/'runtime/media/bin/whisper-cli'
            inputs = dict(source_sha256=source['sha256'], model_sha256=harness.dependency_hash(model),
                          binary_sha256=harness.dependency_hash(binary), language='auto')
            failed = work/'checkpoints'/(fingerprint(['ASR', inputs])+'.json')
            record = read(failed, {})
            if record.get('state') != 'REVIEW_REQUIRED' or record.get('paid') is not False or record.get('error') != 'STAGE_FAILED':
                raise ValueError('STATE_TRANSITION_REJECTED')
            archive = work/'recovery_archive';archive.mkdir(exist_ok=True)
            write(archive/('asr-'+rank+'-'+sha(failed)+'.json'), dict(failed_checkpoint=record, original_analysis=item.get('analysis')))
            store.done(ident, VERSION, rank, key, dict(status='REQUESTED', source_sha256=source['sha256'], local_unpaid_retry=True))
            store.item(ident, rank, status='ASR', operation_inflight=True)
            try:
                future = asr_job(harness, source, model, binary=binary)
                result = future.result()
                if result.get('error'):raise ValueError('SOURCE_ASR_RECOVERY_FAILED')
                path = result.get('transcript') or (result.get('artifacts') or [{}])[0].get('path')
                transcript = read(path, {}) if path else {}
                if not transcript.get('text', '').strip():raise ValueError('NO_RELIABLE_SCRIPT')
                product = job.get('product_profile') or store.product(job['product_profile_id'])
                entry = analyze_sources(manager, ident, harness, job, product, [(source, future)], work, False)[0]
                analysis = entry['analysis']
                original = analysis.get('clean_script') or transcript['text']
                store.item(ident, rank, analysis=analysis, source_sheet=entry['sheet'],
                    source_script=transcript['text'], clean_source_script=analysis.get('clean_script', ''),
                    source_language=transcript.get('language', 'unknown'), voice_quality=analysis['voice_quality'])
                store.done(ident, 'ASR_DONE', rank, source['sha256'], dict(transcript=path))
                draft = review._draft(ident, rank, dict(mode='PASTED', text=original, language='auto'))
                store.done(ident, VERSION, rank, key, dict(status='DRAFT_SAVED', source_sha256=source['sha256'],
                    transcript_sha256=sha(path), requires_real_user_confirmation=True, new_tts_requests=0))
                audit(store, ident, rank, VERSION, status='DRAFT_SAVED', requires_confirmation=True)
                return draft
            except Exception:
                store.item(ident, rank, status=item['status'], operation_inflight=False, issues=item.get('issues', []))
                raise
