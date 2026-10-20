"""Reconcile a checked MP4's record after an in-process code-contract change."""
from pathlib import Path
from .item_review import get_item,audit
from .ownership import checkpoints,final_ready
from .production_policy import approved_content,classify
from .script_language import script,title
from app.s3.headless_runtime import read,sha


def reconcile(manager,id,rank):
    store=manager.store;rank=f'{int(rank):02}'
    with store.lock,manager.lock:
        job,item=get_item(store,id,rank)
        if item.get('status')=='PASS':return
        if job.get('deleted_at') or job.get('control_request') or (id,rank) in manager.item_pending or not approved_content(item):raise ValueError('STATE_TRANSITION_REJECTED')
        if item.get('status')!='FAILED' or item.get('issues')!=['FINAL_NOT_READY']:raise ValueError('STATE_TRANSITION_REJECTED')
        cp=checkpoints(store,id,rank);rendered=cp.get('FINAL_RENDERED',{});checked=cp.get('QA_DONE',{});r=rendered.get('data',{});q=checked.get('data',{});plan=read(item.get('plan'),{})
        output=Path(item.get('output') or '')
        if not output.is_file() or str(output)!=plan.get('output') or r.get('output')!=str(output):raise ValueError('FINAL_OUTPUT_INVALID')
        digest=sha(output)
        if q.get('status') not in {'PASS','WARNING'} or classify(q.get('visual',{}))['status']=='FAIL':raise ValueError('FINAL_NOT_READY')
        tech=q.get('technical',{});arrow=q.get('cta',{})
        if tech.get('status')!='PASS' or tech.get('full_decode_count',0)<1 or arrow.get('status')!='PASS':raise ValueError('FINAL_NOT_READY')
        if any(v.get('output_sha256')!=digest for v in (r,tech,arrow)):raise ValueError('FINAL_OUTPUT_CHANGED')
        work=store.home/'jobs'/id/'work';receipt=read(work/'render'/rendered['input_sha']/'result.json',{})
        if receipt.get('output_sha256')!=digest or receipt.get('output')!=str(output) or read(work/'qa'/rank/'qa.json',{}).get('output_sha256')!=digest:raise ValueError('QA_EVIDENCE_MISSING')
        mismatch=rendered['input_sha']!=checked['input_sha']
        prior=cp.get('FINAL_RECORD_RECONCILIATION',{}).get('data',{})
        if not mismatch and prior.get('output_sha256')!=digest:raise ValueError('STATE_TRANSITION_REJECTED')
        spoken=None if item.get('voiceover_mode')=='MUSIC_ONLY' else script(item)['text']
        if plan.get('final_script')!=spoken or plan.get('title')!=title(item)['text']:raise ValueError('APPROVED_CONTENT_CHANGED')
        for v in [plan['source'],plan['audio'],*plan.get('montage_segments',[])]:
            path=v.get('audio_path') or v.get('path')
            if not path or sha(path)!=v.get('sha256'):raise ValueError('RECOVERY_CACHE_INVALID')
        candidate=dict(cp,QA_DONE=dict(checked,input_sha=rendered['input_sha']))
        if not final_ready(store,job,item,candidate):raise ValueError('FINAL_NOT_READY')
        if mismatch:
            store.done(id,'FINAL_RECORD_RECONCILIATION',rank,digest,dict(previous_qa=checked,render_input_sha=rendered['input_sha'],output_sha256=digest,reason='RENDER_CONTRACT_CHANGED_DURING_QA'))
            store.done(id,'QA_DONE',rank,rendered['input_sha'],q)
        store.item(id,rank,status='CHECKING',issues=[])
        store.item(id,rank,status='PASS',qa_status=q['status'],issues=[])
        audit(store,id,rank,'FINAL_RECORD_RECONCILED',output_sha256=digest,render_reused=True)
        latest=store.get(id)
        if latest['items'] and all(x.get('status')=='PASS' and final_ready(store,latest,x) for x in latest['items']):
            store.update(id,state='COMPLETED',stage='检查')
