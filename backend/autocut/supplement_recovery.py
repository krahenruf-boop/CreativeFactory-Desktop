"""Validated item-bound supplement plans and a durable, item-only resume outbox."""
import copy,json,math,time
from pathlib import Path
from .copy_presentation import digest
from .review_media import inspect_media,base_clips,resolve_supplements,duration_check,SupplementPlan
from .task_instructions import effective
from app.s3.headless_runtime import write

CONTRACT='SUPPLEMENT_RECOVERY_V1'
INVALIDATED=('SUPPLEMENT_PLAN','DURATION_FIT','RENDER_PLAN','SUBTITLE','OVERLAY_LOCKED','FINAL_RENDERED','QA_DONE','FINAL_APPROVED')

def bound_media(j,x):
 # Legacy intake already recorded item downloads; never infer ownership from
 # another item's presence in the job-wide catalog.
 ids={m['id'] for m in x.get('supplement_downloads',[]) if m.get('id')}
 ids.update(c['supplement_source'] for c in (x.get('supplement_plan') or {}).get('clips',[]))
 return [m for m in j.get('supplement_media',[]) if m['id'] in ids]

def validate(j,x,rows):
 result=[];seen=set()
 for row in rows:
  m=inspect_media(row['path'])
  if m['sha256']!=row['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
  if m['sha256'] in seen:continue
  seen.add(m['sha256'])
  result.append(dict(row,**m,job_id=j['id'],item_id=x['rank'],validation_status='PASS',supplement_plan_status='PENDING',created_at=row.get('created_at') or time.time()))
 if not result:raise ValueError('SUPPLEMENT_MEDIA_INVALID')
 return result

def context(j,x,source,analysis,media):
 total=sum(c['source_end']-c['source_start'] for c in base_clips(source,x.get('decision') or {},effective(j,x['rank'])))
 voice=float((x.get('audio_result') or {}).get('duration') or 0)
 if voice<=0:raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
 return dict(contract=CONTRACT,job_id=j['id'],item_id=x['rank'],source_sha256=source['sha256'],product=j.get('product_profile'),source_analysis=analysis,script=x['copy']['localized_script']['text'],available_visual_duration=total,tts_duration=voice,
  minimum_additional_seconds=max(0,math.ceil((voice/1.5-total)*30)/30),preferred_additional_seconds=max(0,math.ceil((voice/1.25-total)*30)/30),
  media=[{k:m[k] for k in ('id','duration','fps','width','height','sha256')} for m in media])

def cache_path(home,id,ctx):return Path(home)/'jobs'/id/'work/supplements/plans'/(digest(ctx)+'.json')

def apply_plan(service,id,rank,plan,media,ctx):
 from .item_review import get_item,audit
 store=service.store;rank=f'{int(rank):02}';j,x=get_item(store,id,rank)
 plan=SupplementPlan.model_validate(plan).model_dump()
 clips=resolve_supplements(x['source_snapshot'],x.get('decision') or {},effective(j,rank),plan,media,ctx['script'])
 fit=duration_check(ctx['tts_duration'],clips,effective(j,rank));key=digest([ctx,plan])
 if x.get('supplement_recovery_key')==key and (x.get('resume_pending') or x.get('resume_running') or x['status']=='PASS'):
  service.manager.resume_job_after_user_action(id);return fit
 ready=fit['fits'];now=time.time()
 manifest=dict(contract=CONTRACT,job_id=id,item_id=rank,media=[dict(m,supplement_plan_status='READY' if ready else 'INSUFFICIENT') for m in media],plan=plan,duration_match=fit,recovery_key=key,updated_at=now)
 manifest_path=store.home/'jobs'/id/'work/supplements'/('manifest-'+rank+'.json')
 # The database is canonical. All invalidation, plan and outbox writes commit
 # together before any worker can observe or execute the new revision.
 with store.lock,store.connect() as db:
  db.execute('BEGIN IMMEDIATE')
  row=db.execute('SELECT data FROM job_items WHERE job_id=? AND rank=?',(id,rank)).fetchone();latest=json.loads(row['data'])
  if latest.get('script_revision')!=x.get('script_revision') or latest.get('audio_result')!=x.get('audio_result'):raise ValueError('STALE_ACTION')
  latest.update(supplement_plan=plan,supplement_manifest=manifest,supplement_manifest_path=str(manifest_path),supplement_downloads=manifest['media'],supplement_recovery_key=key,duration_match=fit,
   plan=None,previous_output=latest.get('output') or latest.get('previous_output'),output=None,qa_status=None,auto_repair_used=False,
   status='QUEUED' if ready else 'WAITING_SUPPLEMENT_MEDIA',issues=[] if ready else ['VISUAL_DURATION_INSUFFICIENT'],
   review_reason=None,waiting_reason=None,review_required=False,operation_inflight=False,resume_phase='SUPPLEMENT',resume_pending=ready,resume_running=False,current_owner='SYSTEM' if ready else 'USER',user_action_status='NONE' if ready else 'ADD_SUPPLEMENT_MEDIA',action_revision=latest.get('action_revision',0)+1)
  db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',(json.dumps(latest,ensure_ascii=False),id,rank))
  marks=','.join('?' for _ in INVALIDATED)
  db.execute(f'DELETE FROM checkpoints WHERE job_id=? AND item IN (?,?) AND stage IN ({marks})',(id,rank,str(int(rank)),*INVALIDATED))
  db.execute("DELETE FROM checkpoints WHERE job_id=? AND stage='VISUAL_QA' AND item IN (?,?)",(id,'preflight-'+rank,'final-'+rank))
  for stage,data in [('SUPPLEMENT_PLAN',manifest),('DURATION_FIT',fit)]:
   db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)',(id,stage,rank,key,json.dumps(data,ensure_ascii=False)))
 write(manifest_path,manifest)
 audit(store,id,rank,'SUPPLEMENT_PLANNED',plan=plan,duration_match=fit,recovery_key=key,invalidated=list(INVALIDATED),resume_pending=ready)
 if ready:service.manager.resume_job_after_user_action(id)
 return fit
