"""Explicit repair of existing completed MP4s, without any upstream execution."""
import copy
from pathlib import Path
from .cta_policy import ensure_plan,required,verify_or_repair,sprite,VERSION,MATCHER_VERSION
from .ownership import checkpoints,invalidate_final
from .item_review import get_item,audit
from app.s3.headless_runtime import read,write,sha,Harness
from app.s3.headless_media import identity
from app.s3.overlay_policy import lock_plan
from scripts.headless_transport import qa_job


def request(manager,id,ranks):
    store=manager.store;rows=[]
    if not isinstance(ranks,list) or not ranks:raise ValueError('INVALID_REQUEST')
    ranks=[f'{int(rank):02}' for rank in ranks]
    if len(set(ranks))!=len(ranks):raise ValueError('INVALID_REQUEST')
    with store.lock,manager.lock:
        job=store.get(id)
        if job.get('deleted_at'):raise ValueError('STATE_TRANSITION_REJECTED')
        for rank in ranks:
            rank=f'{int(rank):02}';_,item=get_item(store,id,rank)
            if (id,rank) in manager.item_pending:continue
            if not item.get('output') or not Path(item['output']).is_file():raise ValueError('FINAL_OUTPUT_INVALID')
            digest=sha(item['output'])
            if item.get('cta_verification',{}).get('output_sha256')==digest and item['cta_verification']['status']=='PASS':continue
            if item.get('cta_attempted_output')==digest:
                # A verifier update may recheck an existing one-shot repair.
                # It is never permission to render another arrow over it.
                folder=Path(item.get('cta_repair_folder',''))
                saved=read(folder/'before.json',{});previous=read(folder/'after/cta-qa.json',{})
                base=Path(item['output']);candidate=base.parent/'CTA_repaired'/(base.stem+'_cta_'+digest[:8]+'.mp4')
                if (item['status']=='FAILED' and item.get('issues')==['CTA_RENDER_FAILED'] and previous
                    and previous.get('matcher')!=MATCHER_VERSION and candidate.is_file() and saved.get('output_sha256')==digest):
                    write(folder/'previous_verification.json',previous)
                    rows.append((rank,folder,digest,True));continue
                raise ValueError('CTA_RENDER_FAILED')
            if item['status'] not in {'PASS','USER_APPROVED'}:raise ValueError('STATE_TRANSITION_REJECTED')
            plan=read(item['plan']);cp=checkpoints(store,id,rank)
            if cp.get('QA_DONE',{}).get('data',{}).get('status') not in {'PASS','WARNING'}:raise ValueError('FINAL_NOT_READY')
            folder=store.home/'jobs'/id/'work/cta_repairs'/rank/digest[:16]
            folder.mkdir(parents=True,exist_ok=True)
            write(folder/'before.json',dict(item=item,plan=plan,checkpoints=cp,output_sha256=digest))
            rows.append((rank,folder,digest,False))
        # Validate the complete request first; no partial queueing on bad input.
        for rank,folder,digest,reverify in rows:
            invalidate_final(store,id,rank)
            store.item(id,rank,status='QUEUED',resume_phase='CTA_REPAIR',resume_pending=True,cta_repair_folder=str(folder),cta_attempted_output=digest,cta_verify_existing=reverify)
        if rows:
            store.update(id,state='RUNNING',stage='CTA')
            manager.enqueue_batch(id,[(rank,'CTA_REPAIR') for rank,_,_,_ in rows])


def resume(manager,id,rank):
    store=manager.store;job,item=get_item(store,id,rank);work=store.home/'jobs'/id/'work'
    folder=Path(item['cta_repair_folder']);saved=read(folder/'before.json');p=copy.deepcopy(saved['plan']);old=saved['item'];base=Path(old['output'])
    try:
        manager.check(id)
        if sha(base)!=saved['output_sha256']:raise ValueError('STALE_ACTION')
        ensure_plan(p)
        rendered=copy.deepcopy(saved['checkpoints']['FINAL_RENDERED']['data'])
        evidence=read(rendered['evidence'])
        # Preserve existing caption raster layers above CTA without regenerating text.
        layers=[l for l in evidence.get('text_layers',[]) if l['start']<p['target_duration'] and l['end']>max(0,p['target_duration']-5)]
        p['cta_preserve_text_layers']=layers
        adjusted=any(l['bbox'][1]<p['visual']['canvas'][1]*.76 for l in layers)
        lock_plan(p)
        directory=base.parent/'CTA_repaired';directory.mkdir(exist_ok=True)
        destination=directory/(base.stem+'_cta_'+saved['output_sha256'][:8]+('_textsafe' if adjusted else '')+'.mp4')
        if item.get('cta_verify_existing') and not destination.is_file():raise ValueError('CTA_RENDER_FAILED')
        store.item(id,rank,status='RENDERING')
        arrow=verify_or_repair(p,base,folder,destination)
        p['output']=arrow['output']
        if arrow['repaired']:p['cta_overlay_from']=arrow['cta_overlay_from']
        lock_plan(p);key=identity(p)
        # Reuse existing subtitle/title raster evidence. No subtitle generation.
        if required(p):
            evidence['cta']=sprite(p,folder/'verified-sprite');evidence['cta']['horizontal_drift_px']=0
        else:evidence['cta']={'enabled':False}
        ep=work/'render'/key/'visual_evidence.json';write(ep,evidence)
        rendered.update(output=p['output'],output_sha256=arrow['output_sha256'],evidence=str(ep),cta_verification=arrow['cta_verification'])
        store.item(id,rank,status='CHECKING')
        with Harness(work,render_workers=1) as h:technical=qa_job(h,p).result()
        if technical.get('status')!='PASS':raise ValueError('CTA_RENDER_FAILED')
        qa=copy.deepcopy(saved['checkpoints']['QA_DONE']['data'])
        qa.update(technical=technical,cta=arrow['cta_verification'])
        if 'visual' in qa:qa['visual']=dict(qa['visual'],cta=True,cta_validation=VERSION)
        write(Path(old['plan']),p)
        store.done(id,'FINAL_RENDERED',rank,key,rendered);store.done(id,'QA_DONE',rank,key,qa)
        store.item(id,rank,output=p['output'],plan=old['plan'],cta_previous_output=str(base),cta_verification=arrow['cta_verification'],
            cta_repair_used=arrow['repaired'],thumbnail=str(work/'qa'/rank/'contact.jpg'),issues=[],qa_status=qa['status'])
        store.item(id,rank,status='PASS',resume_pending=False)
        audit(store,id,rank,VERSION,previous_output=str(base),output=p['output'],repaired=arrow['repaired'],cta=arrow['cta_verification']['status'])
        write(folder/'result.json',dict(status='PASS',repaired=arrow['repaired'],output=p['output'],cta=arrow['cta_verification']))
    except Exception as exc:
        store.item(id,rank,status='FAILED',qa_status='FAIL',issues=['CTA_RENDER_FAILED'],resume_pending=False)
        write(folder/'result.json',dict(status='FAIL',reason=str(exc),previous_output=str(base)))
