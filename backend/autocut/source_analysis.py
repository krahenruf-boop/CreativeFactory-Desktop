"""Two independent source analyses; preserve natural-order editorial grouping."""
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,wait
from app.s3.headless_runtime import read,write,sha
from .task_instructions import effective
from .workbench import selected_script


def resume_failed_analysis(manager,id):
 """Resume only an initial Remix analysis failure, preserving all caches."""
 from .task_instructions import digest
 store=manager.store
 with store.lock,manager.lock:
  job=store.get(id);future=manager.active.get(id)
  if job.get('deleted_at') or job.get('control_request') or (future and not future.done()) or any(j==id for j,_ in manager.item_pending):raise ValueError('STATE_TRANSITION_REJECTED')
  if job['state']!='REVIEW_REQUIRED' or job.get('issues') not in [['AI_DRIVER_EXEC_FAILED'],['AI_DRIVER_TIMEOUT'],['AI_DRIVER_RATE_LIMITED']] or job.get('items') or job.get('mode')!='Remix':raise ValueError('STATE_TRANSITION_REJECTED')
  work=store.home/'jobs'/id/'work';manifest=read(work/'manifest.json',{})
  sources=manifest.get('sources',[])
  if not sources:raise ValueError('RECOVERY_CACHE_INVALID')
  for source in sources:
   if Path(source['path']).resolve().parent!=Path(job['original_input_path']).resolve() or sha(source['path'])!=source['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
  with store.connect() as db:rows=db.execute('SELECT stage,item,input_sha,data FROM checkpoints WHERE job_id=?',(id,)).fetchall()
  if any(r['stage'] not in {'VIDEO_ANALYSIS','SOURCE_ANALYSIS_RESUME'} for r in rows):raise ValueError('STATE_TRANSITION_REJECTED')
  completed={r['item']:r['input_sha'] for r in rows if r['stage']=='VIDEO_ANALYSIS'}
  remaining=[s['rank'] for s in sources if f'{s["rank"]:02}' not in completed]
  if not remaining:raise ValueError('STATE_TRANSITION_REJECTED')
  key=digest([job['issues'],completed])
  if store.checkpoint(id,'SOURCE_ANALYSIS_RESUME','all',key):raise ValueError('RECOVERY_ALREADY_REQUESTED')
  store.done(id,'SOURCE_ANALYSIS_RESUME','all',key,dict(completed_inputs=completed,remaining_source_ranks=remaining,asr_cache_reused=True,copy_or_tts_started=False))
  store.update(id,issues=[])
  # The existing initial pipeline reads its manifest and exact stage caches.
  # No item, copy, approval, audio or final checkpoint exists to reset here.
  manager.enqueue(id)


def analyze_sources(manager,id,h,job,product,asr,work,remix):
 from .pipeline import source_sheet
 completed=set();lock=threading.Lock()
 def one(source,asr_future):
  with manager.control.worker(id):
   from .source_library import POLICY,inspect_source
   if job.get('production_preflight_policy')==POLICY:
    from .source_frame import POLICY as FRAME_POLICY
    source['source_health']=inspect_source(manager.store.home,source,job.get('source_frame_mode') or FRAME_POLICY)
    manager.store.done(id,'SOURCE_HEALTH',source['rank'],source['sha256'],source['source_health'])
    if not remix:manager.store.item(id,source['rank'],source_snapshot=source)
   result=asr_future.result();manager.check(id)
   if result.get('error') and (remix or job.get('script_mode','AUTO')!='AUTO'):
    return dict(source=source,asr_result=result)
   if result.get('error'):result=dict(text='',asr_error=result['error'])
   path=result.get('transcript') or (result.get('artifacts') or [{}])[0].get('path')
   transcript=read(path) if path else result
   if path:write(manager.store.home/'cache/asr'/f'{source["sha256"]}.index.json',dict(path=path,sha256=sha(path)))
   task=effective(job,source['rank']) if not remix else (job.get('resolved_overrides_json') or {}).get('values',{})
   from .source_title_overlay import enabled
   sheet_folder=work/'analysis'/f'{source["rank"]:02}'
   sheet=source_sheet(source,sheet_folder,overlay_policy=True) if enabled(task) else source_sheet(source,sheet_folder)
   inputs=dict(source={k:source[k] for k in ['sha256','duration','width','height']},asr=transcript,product=product,script_reference=job.get('reference',{}),selected_script=selected_script(job,source['rank']),script_reference_mode=task.get('script_reference_mode',job.get('script_reference_mode','smart')),watermark_mode=task.get('watermark_mode_override',job['watermark_mode']))
   intent={k:v for k,v in task.items() if k in {'script_reference_mode','reference_selector','preserve_source_structure','watermark_mode_override','source_title_overlay_policy'}}
   if intent:inputs['task_overrides']=intent
   analysis=manager.judge(id,h,'VIDEO_ANALYSIS',source['rank'],inputs,'analyze',[sheet])
   if source.get('source_health'):
    source['source_health']=dict(source['source_health'],analysis_status='ANALYZED')
    manager.store.done(id,'SOURCE_HEALTH',source['rank'],source['sha256'],source['source_health'])
    if not remix:manager.store.item(id,source['rank'],source_snapshot=source)
   with lock,manager.store.lock:
    completed.add(source['rank'])
    manager.store.update(id,analyzed_count=max(manager.store.get(id).get('analyzed_count',0),len(completed)))
   return dict(source=source,asr_result=result,transcript_path=path,transcript=transcript,sheet=sheet,task=task,analysis=analysis)
 pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='source-analysis')
 try:
  futures=[pool.submit(one,s,f) for s,f in asr];results=[]
  for future in futures:
   # The parent must park too; blocking on a parked child would prevent PAUSED.
   while not future.done():manager.check(id);wait([future],timeout=.2)
   manager.check(id);results.append(future.result())
  return results
 finally:pool.shutdown(wait=True,cancel_futures=True)
