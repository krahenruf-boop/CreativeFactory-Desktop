"""Resume interrupted AI judgments without exporting completed media again."""
import re
from pathlib import Path
from .item_review import get_item,audit
from .ownership import checkpoints,final_ready
from .production_policy import approved_content,classify,VERSION
from .script_language import script,title,check,target_language
from .original_text_cover import review_issues
from app.s3.headless_runtime import read,sha,Harness

ERRORS={'AI_DRIVER_EXEC_FAILED','AI_DRIVER_TIMEOUT','AI_DRIVER_UNAVAILABLE','AI_DRIVER_RATE_LIMITED'}

def render_evidence(item):
    plan=read(item.get('plan'),{})
    if not plan or str(plan.get('rank'))!=item['rank']:raise ValueError('RECOVERY_CACHE_INVALID')
    spoken=None if item.get('voiceover_mode')=='MUSIC_ONLY' else script(item)['text']
    if plan.get('final_script')!=spoken or plan.get('title')!=title(item)['text']:raise ValueError('APPROVED_CONTENT_CHANGED')
    if Path(plan['output']).exists():raise ValueError('FINAL_OUTPUT_ALREADY_EXISTS')
    for entry in [plan.get('source',{}),plan.get('audio',{}),*(plan.get('montage_segments') or [])]:
        path=entry.get('path') or entry.get('audio_path')
        if not path or not Path(path).is_file() or sha(path)!=entry.get('sha256'):raise ValueError('RECOVERY_CACHE_INVALID')
    return plan

def final_evidence(store,job,item):
    work=store.home/'jobs'/job['id']/'work';rank=item['rank']
    cp=checkpoints(store,job['id'],rank);rendered=cp.get('FINAL_RENDERED',{})
    plan=read(item.get('plan'),{})
    technical=read(work/'qa'/rank/'qa.json',{})
    arrow=rendered.get('data',{}).get('cta_verification') or read(work/'cta_qa'/rank/'before/cta-qa.json',{})
    output=Path(item.get('output') or '')
    if not rendered or not output.is_file() or plan.get('output')!=str(output):raise ValueError('FINAL_OUTPUT_INVALID')
    digest=sha(output)
    if any(d.get('output_sha256')!=digest for d in [rendered['data'],technical,arrow]):raise ValueError('FINAL_OUTPUT_CHANGED')
    if technical.get('status')!='PASS' or technical.get('full_decode_count',0)<1 or arrow.get('status')!='PASS':raise ValueError('FINAL_NOT_READY')
    if plan.get('final_script')!=script(item)['text'] or plan.get('title')!=title(item)['text']:raise ValueError('APPROVED_CONTENT_CHANGED')
    if not (work/'qa'/rank/'contact.jpg').is_file():raise ValueError('QA_EVIDENCE_MISSING')
    return work,plan,rendered,technical,arrow

def request(manager,id,ranks):
    store=manager.store;rows=[]
    with store.lock,manager.lock:
        job=store.get(id)
        if job.get('deleted_at') or job.get('control_request') or (job.get('batch_review') or {}).get('phase')!='RUNNING':raise ValueError('STATE_TRANSITION_REJECTED')
        if not isinstance(ranks,list) or not ranks:raise ValueError('INVALID_REQUEST')
        ranks=[f'{int(rank):02}' for rank in ranks]
        if len(ranks)!=len(set(ranks)):raise ValueError('INVALID_REQUEST')
        for rank in ranks:
            _,item=get_item(store,id,rank)
            if (id,rank) in manager.item_pending or item['status']=='PASS':continue
            if item['status']!='FAILED' or len(item.get('issues') or [])!=1 or item['issues'][0] not in ERRORS:raise ValueError('STATE_TRANSITION_REJECTED')
            if not approved_content(item):raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
            check(script(item),target_language(job));check(title(item),target_language(job),'TITLE')
            if item.get('output') or 'FINAL_RENDERED' in checkpoints(store,id,rank):
                final_evidence(store,job,item);phase='AI_FINAL_QA'
            elif item.get('plan'):
                render_evidence(item);phase='AI_RENDER_QA'
            else:
                # Existing item resume consumes cached analysis/copy and never
                # reassigns scripts. Only missing downstream stages are run.
                source=item.get('source_snapshot') or {}
                if not item.get('analysis') or not item.get('copy',{}).get('titles') or not Path(source.get('path','')).is_file():raise ValueError('RECOVERY_CACHE_INVALID')
                if sha(source['path'])!=source.get('sha256'):raise ValueError('SOURCE_HASH_CHANGED')
                phase='SCRIPT'
            rows.append((rank,phase))
        # Validate all requested items before changing any state. Keep final
        # checkpoints: they are the proof that QA-only recovery may reuse MP4s.
        for rank,phase in rows:store.item(id,rank,status='QUEUED',issues=[],resume_pending=True,resume_phase=phase)
        if rows:
            store.update(id,state='RUNNING',stage='检查')
            manager.enqueue_batch(id,rows)

def resume_render(manager,id,rank):
    store=manager.store;job,item=get_item(store,id,rank)
    try:
        manager.check(id)
        if not approved_content(item):raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
        plan=render_evidence(item);work=store.home/'jobs'/id/'work'
        store.item(id,rank,status='OVERLAY',issues=[])
        with Harness(work,render_workers=1) as harness:
            manager.finish_plans(id,harness,job,[plan],work)
        audit(store,id,rank,'AI_RENDER_QA_RESUMED',status=get_item(store,id,rank)[1]['status'],plan_reused=True)
    except Exception as exc:
        from .pipeline import Stopped
        if isinstance(exc,Stopped):raise
        code=getattr(exc,'code',str(exc));code=code if re.fullmatch('[A-Z][A-Z0-9_:,-]{1,160}',code) else 'RENDER_QA_RECOVERY_FAILED'
        store.item(id,rank,status='FAILED',qa_status='FAIL',issues=[code])
        audit(store,id,rank,'AI_RENDER_QA_RECOVERY_FAILED',error=code)

def resume_final(manager,id,rank):
    store=manager.store;job,item=get_item(store,id,rank)
    try:
        manager.check(id)
        if not approved_content(item):raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
        work,plan,rendered,technical,arrow=final_evidence(store,job,item)
        store.item(id,rank,status='CHECKING')
        from .qa_evidence import review_images
        images,coverage=review_images(work,rank,plan,technical)
        with Harness(work,render_workers=1) as harness:
            review=manager.judge(id,harness,'VISUAL_QA','final-'+rank,dict(plan=plan,technical_issues=technical.get('issues',[]),output_sha256=technical['output_sha256'],retained_source=job.get('retain_original',False),evidence_coverage=coverage),'review_final',images)
        verdict=classify(review);text_issues=review_issues(plan,review)
        if text_issues:raise ValueError(text_issues[0])
        if verdict['status']=='FAIL':
            store.done(id,'QA_DONE',rank,rendered['input_sha'],dict(technical=technical,visual=review,status='FAIL',cta=arrow))
            raise ValueError('SEVERE_VISUAL_QA')
        warnings=list(dict.fromkeys((item.get('qa_warnings') or [])+verdict['issues']))
        status='WARNING' if warnings or verdict['status']=='WARNING' else 'PASS'
        store.done(id,'QA_DONE',rank,rendered['input_sha'],dict(technical=technical,visual=review,status=status,policy=VERSION,warnings=warnings,cta=arrow))
        store.item(id,rank,qa_status=status,qa_warnings=warnings,issues=[],review_reason=None,cta_verification=arrow,thumbnail=str(work/'qa'/rank/'contact.jpg'))
        latest,current=get_item(store,id,rank)
        if not final_ready(store,latest,current):raise ValueError('FINAL_NOT_READY')
        store.item(id,rank,status='PASS',production_policy=VERSION)
        audit(store,id,rank,'AI_FINAL_QA_RESUMED',output=plan['output'],output_sha256=technical['output_sha256'],render_reused=True)
    except Exception as exc:
        from .pipeline import Stopped
        if isinstance(exc,Stopped):raise
        code=getattr(exc,'code',str(exc));code=code if re.fullmatch('[A-Z][A-Z0-9_:,-]{1,160}',code) else 'FINAL_QA_RECOVERY_FAILED'
        store.item(id,rank,status='FAILED',qa_status='FAIL',issues=[code])
        audit(store,id,rank,'AI_FINAL_QA_RECOVERY_FAILED',error=code)
