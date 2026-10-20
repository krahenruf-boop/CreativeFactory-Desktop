"""Explicit repair of existing completed MP4s, without any upstream execution."""
import copy,subprocess
from pathlib import Path
from .cta_policy import ensure_plan,required,verify,verify_or_repair,sprite,VERSION,MATCHER_VERSION,POSITION_POLICY,LEFT_ANCHOR
from .context import RES
from .ownership import checkpoints,invalidate_final
from .item_review import get_item,audit
from app.s3.headless_runtime import read,write,sha,Harness
from app.s3.headless_media import identity,render_plan
from app.s3.overlay_policy import lock_plan
from scripts.headless_transport import qa_job


def text_signature(evidence):
    """Compare frozen text pixels and timing, ignoring only generated file paths."""
    return [{k:v for k,v in layer.items() if k!='layer'} for layer in evidence.get('text_layers',[])]


def relocate_from_source(plan,base,output,folder,evidence):
    """Remove the old rendered arrow by rebuilding from verified original media.

    Reuse the existing edit/timing/text plan. Copy the finished audio stream,
    never synthesize speech, and reject any title/subtitle pixel changes.
    """
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);output=Path(output)
    key=identity(plan);video=folder/'source-render.mp4';receipt=folder/'relocation.json'
    for layer in evidence.get('text_layers',[]):
        if sha(layer['layer'])!=layer['layer_sha256']:raise ValueError('CTA_EXISTING_TEXT_CHANGED')
    if output.exists():
        old=read(receipt,{})
        if old.get('plan_key')!=key or old.get('output_sha256')!=sha(output) or old.get('base_sha256')!=sha(base):
            raise ValueError('CTA_RELOCATION_OUTPUT_CHANGED')
        rendered=old['rendered']
    else:
        cached=read(folder/'render/result.json',{})
        if video.is_file():
            if cached.get('output_sha256')!=sha(video) or read(folder/'render-plan.json',{}).get('key')!=key:
                raise ValueError('CTA_RELOCATION_CACHE_CHANGED')
            rendered=cached
        else:
            write(folder/'render-plan.json',dict(key=key))
            rendered=render_plan(plan,video,folder/'render')
        if text_signature(read(rendered['evidence']))!=text_signature(evidence):
            raise ValueError('CTA_EXISTING_TEXT_CHANGED')
        partial=output.with_suffix('.partial.mp4')
        subprocess.run([str(RES/'runtime/media/bin/ffmpeg'),'-nostdin','-v','error','-y',
            '-i',str(video),'-i',str(base),'-map','0:v:0','-map','1:a:0','-c','copy',
            '-movflags','+faststart',str(partial)],check=True,capture_output=True,timeout=120)
        partial.replace(output)
        write(receipt,dict(plan_key=key,base_sha256=sha(base),output_sha256=sha(output),rendered=rendered))
    result=verify(plan,output,folder/'after')
    if result['status']!='PASS':raise ValueError('CTA_LEFT_RELOCATION_FAILED')
    # Only the validated export in the source folder is a deliverable.
    video.unlink(missing_ok=True)
    return dict(output=str(output),output_sha256=sha(output),cta_verification=result,repaired=True,
        cta_rebuilt_from_source=True,evidence=rendered['evidence'])


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
            plan=read(item['plan']);proof=item.get('cta_verification',{})
            if (proof.get('output_sha256')==digest and proof.get('status')=='PASS'
                and proof.get('matcher')==MATCHER_VERSION and proof.get('position_policy')==POSITION_POLICY
                and proof.get('right_matched_frames')==0 and plan['visual']['cta'].get('anchor_x')==LEFT_ANCHOR):continue
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
            cp=checkpoints(store,id,rank)
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
        relocate=required(p) and p['visual']['cta'].get('anchor_x',1)>.5
        ensure_plan(p)
        rendered=copy.deepcopy(saved['checkpoints']['FINAL_RENDERED']['data'])
        evidence=read(rendered['evidence'])
        # Preserve existing caption raster layers above CTA without regenerating text.
        layers=[l for l in evidence.get('text_layers',[]) if l['start']<p['target_duration'] and l['end']>max(0,p['target_duration']-5)]
        p['cta_preserve_text_layers']=layers
        adjusted=any(l['bbox'][1]<p['visual']['canvas'][1]*.76 for l in layers)
        lock_plan(p)
        directory=base.parent if relocate else base.parent/'CTA_repaired';directory.mkdir(exist_ok=True)
        destination=directory/(base.stem+('_left_' if relocate else '_cta_')+saved['output_sha256'][:8]+('_textsafe' if adjusted else '')+'.mp4')
        if item.get('cta_verify_existing') and not destination.is_file():raise ValueError('CTA_RENDER_FAILED')
        store.item(id,rank,status='RENDERING')
        arrow=(relocate_from_source(p,base,destination,folder/'left-only',evidence) if relocate
            else verify_or_repair(p,base,folder,destination))
        p['output']=arrow['output']
        if arrow.get('cta_rebuilt_from_source'):
            p.pop('cta_overlay_from',None)
            p['cta_rebuilt_from_source']=dict(previous_output=str(base),previous_sha256=saved['output_sha256'],policy=POSITION_POLICY)
            evidence=read(arrow['evidence'])
        elif arrow['repaired']:p['cta_overlay_from']=arrow['cta_overlay_from']
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
