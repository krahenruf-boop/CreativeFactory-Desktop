"""Remove a verified short black source tail from a failed two-shot plan only."""
import copy, json, re, subprocess
from .ai_recovery import render_evidence
from .item_review import get_item, audit
from .production_policy import approved_content
from .task_instructions import digest
from .parallel_tasks import resource_slot
from app.s3.headless_runtime import write
from app.s3.overlay_policy import lock_plan

VERSION='SOURCE_BLACK_TAIL_RECOVERY_V1'


def black_intervals(path):
    with resource_slot('cpu'):
        result=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-v','info','-i',str(path),
            '-vf','blackdetect=d=0.01:pix_th=0.10','-an','-f','null','-'],capture_output=True,text=True,timeout=180)
    if result.returncode:raise ValueError('SOURCE_BLACK_TAIL_UNVERIFIED')
    return [(float(a),float(b)) for a,b in re.findall(r'black_start:([0-9.]+) black_end:([0-9.]+)',result.stderr)]


def repaired_plan(original, intervals, donor_black):
    plan=copy.deepcopy(original);segments=plan.get('montage_segments') or []
    if len(segments)!=2 or plan.get('video_speed')!=1 or any(s.get('speed')!=1 for s in segments):raise ValueError('STATE_TRANSITION_REJECTED')
    first,donor=segments;end=first['source_end']
    tails=[a for a,b in intervals if first['source_start']<a<end and b>=end-1/30-.0001]
    if len(tails)!=1:raise ValueError('SOURCE_BLACK_TAIL_UNVERIFIED')
    cutoff=round(tails[0]*30)/30;removed=end-cutoff
    if not 1/30-.0001<=removed<=.5 or cutoff-first['source_start']<.5:raise ValueError('SOURCE_BLACK_TAIL_UNVERIFIED')
    if donor['source_end']+removed>donor['source_duration'] or any(a<donor['source_end']+removed and donor['source_start']<b for a,b in donor_black):raise ValueError('SOURCE_BLACK_TAIL_UNVERIFIED')
    if abs(first['output_end']-donor['output_start'])>.001 or abs(donor['output_end']-plan['target_duration'])>.001:raise ValueError('STATE_TRANSITION_REJECTED')
    first['source_end']=cutoff;first['output_end']-=removed
    donor['source_end']+=removed;donor['output_start']-=removed
    plan['source_tail_recovery']=dict(version=VERSION,source_sha256=first['sha256'],source_black_interval=[cutoff,end],removed_seconds=removed,donor_sha256=donor['sha256'],donor_added_seconds=removed,audio_unchanged=True)
    return plan


def request(manager,ident,rank):
    store=manager.store;rank=f'{int(rank):02}'
    with store.lock,manager.lock:
        job,item=get_item(store,ident,rank)
        if job.get('deleted_at') or job.get('control_request') or manager.control.busy(ident) or item['status']!='FAILED' or item.get('issues')!=['SEVERE_VISUAL_QA'] or not approved_content(item):raise ValueError('STATE_TRANSITION_REJECTED')
        original=render_evidence(item);key=digest([VERSION,original])
        if store.checkpoint(ident,VERSION,rank,key):return
        with store.connect() as db:
            row=db.execute("SELECT data FROM checkpoints WHERE job_id=? AND stage='VISUAL_QA' AND item=?",(ident,'preflight-'+rank)).fetchone()
        review=json.loads(row['data']) if row else {}
        if review.get('severity')!='SEVERE' or 'black' not in str(review.get('issues',[])).lower():raise ValueError('SOURCE_BLACK_TAIL_UNVERIFIED')
        segments=original.get('montage_segments') or []
        if len(segments)!=2:raise ValueError('STATE_TRANSITION_REJECTED')
        intervals=black_intervals(segments[0]['path']);donor_black=black_intervals(segments[1]['path'])
        plan=repaired_plan(original,intervals,donor_black);lock_plan(plan)
        work=store.home/'jobs'/ident/'work';archive=work/'recovery_archive';archive.mkdir(exist_ok=True)
        write(archive/('black-tail-'+rank+'-'+key+'.json'),dict(original_plan=original,failed_review=review,source_black_intervals=intervals,donor_black_intervals=donor_black))
        write(item['plan'],plan)
        store.done(ident,VERSION,rank,key,plan['source_tail_recovery'])
        store.done(ident,'RENDER_PLAN',rank,digest(plan),dict(path=item['plan'],recovery=VERSION))
        store.item(ident,rank,status='QUEUED',issues=[],resume_pending=True,resume_phase='AI_RENDER_QA')
        audit(store,ident,rank,VERSION,**plan['source_tail_recovery'])
        store.update(ident,state='RUNNING',stage='剪辑');manager.enqueue_batch(ident,[(rank,'AI_RENDER_QA')])
