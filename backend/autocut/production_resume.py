"""Durable approval outbox and bounded legacy recovery, shared by both barriers."""
import json
from .production_policy import VERSION,approved_content,ordinary_legacy
from .batch_confirmation import batch_scope,valid_title
from .ownership import checkpoints,final_ready
from .script_language import check,script,target_language

def recheck_cta_layers(manager,id,ranks):
 """Resume collision-only preflight failures from their frozen render plans."""
 from pathlib import Path
 from app.s3.headless_runtime import read,sha
 from .script_language import title
 from .ownership import invalidate_final
 store=manager.store;rows=[]
 with store.lock,manager.lock:
  j=store.get(id)
  if j.get('deleted_at') or j.get('control_request') or (j.get('batch_review') or {}).get('phase')!='RUNNING':raise ValueError('STATE_TRANSITION_REJECTED')
  if not isinstance(ranks,list) or not ranks:raise ValueError('INVALID_REQUEST')
  ranks=[f'{int(r):02}' for r in ranks]
  if len(ranks)!=len(set(ranks)):raise ValueError('INVALID_REQUEST')
  for rank in ranks:
   x=next((x for x in j['items'] if x['rank']==rank),None)
   if not x or not approved_content(x):raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
   if (id,rank) in manager.item_pending or x.get('status')=='PASS':continue
   if x['status']!='FAILED' or x.get('issues')!=['CTA_SUBTITLE_COLLISION']:raise ValueError('STATE_TRANSITION_REJECTED')
   p=read(x['plan'])
   if str(p['rank'])!=rank or p.get('final_script')!=script(x)['text'] or p['title']!=title(x)['text']:raise ValueError('APPROVED_CONTENT_CHANGED')
   if Path(p['output']).exists():raise ValueError('FINAL_OUTPUT_ALREADY_EXISTS')
   for k in ('source','audio'):
    entry=p[k];path=entry.get('path') or entry.get('audio_path')
    if not path or not Path(path).is_file() or sha(path)!=entry['sha256']:raise ValueError('RECOVERY_CACHE_INVALID')
   rows.append(rank)
  # No queue/state mutation until every requested item has been validated.
  for rank in rows:
   invalidate_final(store,id,rank)
   store.item(id,rank,status='QUEUED',issues=[],resume_pending=True,resume_phase='CTA_TEXT_LAYERS',cta_layer_recovery='BELOW_TEXT')
  if rows:
   store.update(id,state='RUNNING',stage='剪辑')
   manager.enqueue_batch(id,[(rank,'CTA_TEXT_LAYERS') for rank in rows])

def recheck_visual_severity(manager,id,ranks):
 """Explicit onsite repair of old boolean-only QA, never automatic global retry."""
 store=manager.store;contract='VISUAL_SEVERITY_V1'
 with store.lock:
  j=store.get(id)
  if j.get('deleted_at') or (j.get('batch_review') or {}).get('phase')!='RUNNING':raise ValueError('STATE_TRANSITION_REJECTED')
  if not isinstance(ranks,list) or not ranks or len(ranks)!=len(set(ranks)):raise ValueError('INVALID_REQUEST')
  rows=[]
  for rank in ranks:
   x=next((x for x in j['items'] if x['rank']==rank),None)
   if not x or not approved_content(x):raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
   if x.get('visual_qa_recheck_contract')==contract:continue
   if x['status']!='FAILED' or x.get('issues')!=['SEVERE_VISUAL_QA']:raise ValueError('STATE_TRANSITION_REJECTED')
   with store.connect() as db:
    old=[json.loads(r['data']) for r in db.execute("SELECT data FROM checkpoints WHERE job_id=? AND stage='VISUAL_QA' AND item IN (?,?)",(id,'preflight-'+rank,'final-'+rank))]
   if not old or any(r.get('severity') not in {None,'UNKNOWN'} for r in old):raise ValueError('STATE_TRANSITION_REJECTED')
   rows.append(rank)
  # Validation of the complete requested set precedes any mutation.
  with store.connect() as db:
   db.execute('BEGIN IMMEDIATE')
   for rank in rows:
    row=db.execute('SELECT data FROM job_items WHERE job_id=? AND rank=?',(id,rank)).fetchone();x=json.loads(row['data'])
    x.update(resume_pending=True,resume_phase='VISUAL',visual_qa_recheck_contract=contract)
    db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',(json.dumps(x,ensure_ascii=False),id,rank))
 return manager.resume_job_after_user_action(id)

def resume(manager,id,recover=False):
 store=manager.store
 with store.lock:
  j=store.get(id)
  if j.get('deleted_at') or j.get('control_request') or j['state'] in {'COMPLETED','PAUSING','PAUSED','STOPPING','STOPPED'}:return j
  phase,ranks=batch_scope(j)
  if phase=='TITLES':
   missing=[x for x in j['items'] if x['rank'] in ranks and (len(x.get('title_candidates') or [])!=3 or not all(valid_title(t,j) for t in x['title_candidates']))]
   if missing and (not j.get('title_prepare_failed')):manager.prepare_batch_titles(id,[x['rank'] for x in missing])
   return store.get(id)
  if phase!='RUNNING':return j
  rows=[]
  for x in j['items']:
   if x['status'] in {'PASS','USER_APPROVED'} or not approved_content(x):continue
   legacy=recover and x.get('production_policy')!=VERSION and (ordinary_legacy(x) or x['status']=='WAITING_FINAL_REVIEW' or x['status']=='WAITING_SUPPLEMENT_MEDIA' and x.get('issues')==['VISUAL_DURATION_INSUFFICIENT'] or x['status'] in {'WAITING_SCRIPT_APPROVAL','WAITING_TITLE_APPROVAL'} or x.get('issues')==['UNKNOWN_PERFORMANCE_STAGE'])
   pending=x.get('resume_pending') or recover and j['state']=='INTERRUPTED' and (x.get('resume_running') or x['status'] in {'QUEUED','VOICE','EDITING','OVERLAY','RENDERING','CHECKING','DURATION_FIT','SCRIPTING'})
   if not (legacy or pending):continue
   try:
    if x.get('voiceover_mode')!='MUSIC_ONLY':check(script(x),target_language(j),require_metadata=True)
    check(x.get('final_title') or {},target_language(j),'TITLE',require_metadata=True)
   except ValueError:
    store.item(id,x['rank'],status='FAILED',issues=['FINAL_SCRIPT_LANGUAGE_MISMATCH'],resume_pending=False,production_policy=VERSION);continue
   # Completed, valid warning-only output can be finalized without new render.
   cp=checkpoints(store,id,x['rank'])
   if legacy and 'QA_DONE' in cp and cp['QA_DONE']['data'].get('technical',{}).get('status')=='PASS':
    from .production_policy import classify
    qa=cp['QA_DONE']['data'];verdict=classify(qa.get('visual') or qa)
    if verdict['status']!='FAIL':
     store.done(id,'QA_DONE',x['rank'],cp['QA_DONE']['input_sha'],dict(qa,status=verdict['status']))
     if final_ready(store,j,x):
      store.item(id,x['rank'],status='PASS',qa_status=verdict['status'],qa_warnings=verdict['issues'],issues=[],production_policy=VERSION);continue
   if (id,x['rank']) in manager.item_pending:continue
   store.item(id,x['rank'],status='QUEUED',issues=[],review_reason=None,waiting_reason=None,review_required=False,resume_pending=True,resume_running=False,production_policy=VERSION,current_owner='SYSTEM',user_action_status='NONE')
   rows.append((x['rank'],x.get('resume_phase','TITLE')))
  if rows:
   store.update(id,state='QUEUED',stage='配音',script_confirmation_pending=False,title_confirmation_pending=False,production_policy=VERSION)
   manager.enqueue_batch(id,rows)
  return store.get(id)
