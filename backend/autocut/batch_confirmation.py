"""Job-level human barriers; item production remains independent after release.

Read-only projection for legacy jobs, all-or-nothing snapshot/checkpoint writes,
request receipts for retries. No provider or renderer implementation lives here.
"""
import copy,json,time
from .content_confirmation import content_view
from .ownership import item_state,digest
from .script_language import check,script,script_fields,target_language,normalize_response,VERSION
from .copy_presentation import chinese,TranslationCache

ERRORS={'BATCH_NOT_READY':'还有素材尚未准备好，请检查标记的行','BATCH_TITLE_REQUIRED':'请为每条视频选择一个目标语言标题','BATCH_STAGE_INVALID':'请先完成当前整批确认','BATCH_REQUEST_INVALID':'提交内容不完整，请刷新后重试'}
WAITING={'WAITING_SCRIPT_APPROVAL','WAITING_TITLE_APPROVAL'}

def batch_scope(j):
 b=j.get('batch_review') or {}
 if b:return b['phase'],b['ranks']
 if j.get('batch_review_required'):return 'SCRIPTS',[x['rank'] for x in j['items']]
 pending=[x for x in j['items'] if x.get('status') in WAITING or x.get('content_edit')]
 if pending:
  # Never pull an already-running/completed legacy item back across a barrier.
  rows=[x for x in j['items'] if x in pending or x.get('status')=='REVIEW_REQUIRED' and not x.get('output')]
  return ('TITLES' if all(x.get('script_status')=='USER_APPROVED' and x.get('status')=='WAITING_TITLE_APPROVAL' for x in rows) else 'SCRIPTS'),[x['rank'] for x in rows]
 return 'RUNNING',[]

def script_ready(j,x,c):
 if x.get('operation_inflight'):return False,'口播正在准备中'
 if x.get('status') in {'ANALYZING','ASR','SCRIPTING','LOCALIZING','QUEUED'} and j.get('state') in {'RUNNING','QUEUED','STOPPING'}:return False,'口播正在准备中'
 d=x.get('script_draft') if c['draft_id'] else None
 if d and d.get('base_revision')!=x.get('script_revision',0):return False,'脚本已变化，请重新选择'
 if c['voiceover_mode']=='MUSIC_ONLY':return (True,'') if (d or x).get('music') else (False,'请选择本地音乐')
 if not (c['script'] or {}).get('text'):return False,'暂无可用口播，请更换脚本'
 try:check(c['script'] or {},target_language(j),require_metadata=True)
 except ValueError:return False,'脚本语言异常或尚未准备完成'
 return (True,'') if chinese((c['script'] or {}).get('text_zh')) else (False,'中文对照暂不可用')

def recovered_copy(store,j,x):
 """Recover a legacy prepared response, by exact spoken text, without AI calls.

 0.1.13 saved final_script before the editor's wording review, but only saved
 copy/titles after that review. Its per-job response cache still owns the titles.
 """
 try:
  saved=json.loads((store.home/'jobs'/j['id']/'work/scripts/copy_results.json').read_text())
  for row in saved.values():
   try:response=normalize_response(row.get('response') or {},j)
   except (ValueError,TypeError,AttributeError):continue
   if response['localized_script']['text']==(script(x) or {}).get('text') and len(response.get('titles',[]))==3:return response
 except (OSError,ValueError,TypeError,AttributeError):pass
 return {}

def valid_title(t,j):
 try:check(t or {},target_language(j),'TITLE',require_metadata=True)
 except ValueError:return False
 return chinese((t or {}).get('text_zh'))

def batch_view(store,j,states=None):
 phase,ranks=batch_scope(j);cache=TranslationCache(store.home);rows=[]
 for x in j['items']:
  c=content_view(j,x,cache);state=(states or {}).get(x['rank']) or item_state(store,j,x)
  ready,reason=script_ready(j,x,c)
  titles=c['titles'];titles_ok=len(titles)==3 and len({str(t.get('id')) for t in titles})==3 and all(valid_title(t,j) for t in titles)
  selected=x.get('batch_selected_title_id',c['default_title_id'])
  options=titles+([c['custom_title']] if c['custom_title'] else [])
  title_ok=titles_ok and any(valid_title(t,j) and t.get('preflight',{}).get('status')!='BLOCKED' for t in options) and not x.get('operation_inflight')
  from .production_preflight import review_duration
  preflight=review_duration(store.home,j,x,c) if x['rank'] in ranks else dict(status='NOT_APPLICABLE')
  rows.append(dict(rank=x['rank'],script=c['script'],script_zh=(c['script'] or {}).get('text_zh'),voiceover_mode=c['voiceover_mode'],script_ready=ready,script_error=reason,title_candidates=titles,custom_title=c['custom_title'],selected_title=selected,title_ready=title_ok,production_preflight=preflight,source_health=(x.get('source_snapshot') or {}).get('source_health'),in_scope=x['rank'] in ranks,action_token=state['action_token'],thumbnail=bool(x.get('thumbnail') or x.get('source_sheet')),duration=x.get('duration') or x.get('source_duration'),status=x['status'],pipeline_status=state['pipeline_status']))
 scoped=[x for x in rows if x['in_scope']];expected=int((j.get('output_count') if j.get('mode')=='Remix' else j.get('source_count')) or len(rows)) if j.get('batch_review_required') and not j.get('batch_review') else len(scoped)
 complete=bool(scoped) and len(scoped)==expected
 all_scripts=complete and all(x['script_ready'] for x in scoped)
 all_titles=complete and all(x['title_ready'] for x in scoped)
 token=digest([phase,ranks,expected,[(x['rank'],x['action_token'],x['script'],x['title_candidates'],x['custom_title']) for x in scoped]])
 pending=phase in {'SCRIPTS','TITLES'};busy=j['state'] in {'RUNNING','QUEUED'} or any(x.get('operation_inflight') for x in j['items'] if x['rank'] in ranks)
 completed=sum(x['pipeline_status']=='COMPLETED' for x in rows)
 # Missing review content stays at the human barrier, with its row-level reason
 # and repair action. Only the production phase uses the failure fallback.
 if phase=='SCRIPTS':label='待确认口播' if all_scripts else '分析中' if busy else '口播待补齐'
 elif phase=='TITLES':label='待确认标题' if all_titles else '正在准备标题' if busy else '标题待补齐'
 else:label='已完成' if rows and completed==len(rows) else '剪辑中' if j['state'] in {'RUNNING','QUEUED'} or any(x['status']=='QUEUED' for x in rows) else ('需要处理 '+str(len(rows)-completed)) if completed else '失败'
 actions=[] if j.get('deleted_at') else ['APPROVE_SCRIPTS'] if phase=='SCRIPTS' and all_scripts else ['APPROVE_TITLES'] if phase=='TITLES' and all_titles else []
 return dict(phase=phase,ranks=ranks,items=rows,total=len(rows),expected=expected,script_ready_count=sum(x['script_ready'] for x in scoped),title_ready_count=sum(x['title_ready'] for x in scoped),all_scripts_ready=all_scripts,all_titles_ready=all_titles,allowed_actions=actions,action_token=token,label='已删除' if j.get('deleted_at') else label,legacy_partial=pending and len(ranks)!=len(rows))

def initial_batch(j):return batch_scope(j)[0] in {'SCRIPTS','TITLES'}

class BatchConfirmation:
 def __init__(self,store,manager,content):self.store=store;self.manager=manager;self.content=content
 def view(self,id):return batch_view(self.store,self.store.get(id))
 def observe(self,j,updates,state):
  from .runtime_insights import transition
  policy=j.get('production_preflight_policy')
  for rank,row in updates.items():
   old=next(x for x in j['items'] if x['rank']==rank)
   transition(self.store.home,j['id'],rank,old['status'],row['status'],policy)
  transition(self.store.home,j['id'],'job',j['state'],state,policy)
 def validate(self,id,phase,data):
  j=self.store.get(id);b=self.view(id);request=data.get('request_id')
  if j.get('deleted_at'):raise ValueError('JOB_DELETED')
  if not isinstance(request,str) or not 8<=len(request)<=100:raise ValueError('BATCH_REQUEST_INVALID')
  receipt=(j.get('batch_review') or {}).get('receipts',{}).get(request)
  if receipt:
   if receipt!=digest([phase,data]):raise ValueError('STALE_ACTION')
   return j,b,True
  if b['phase']!=phase:raise ValueError('BATCH_STAGE_INVALID')
  if data.get('action_token')!=b['action_token']:raise ValueError('STALE_ACTION')
  return j,b,False
 def commit(self,db,j,b,data,phase,updates,stages):
  raw=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(j['id'],)).fetchone()[0]);meta=copy.deepcopy(j.get('batch_review') or dict(version=1,ranks=b['ranks'],receipts={}))
  meta['receipts'][data['request_id']]=digest([b['phase'],data]);meta['phase']=phase
  raw.update(script_confirmation_pending=False,title_confirmation_pending=phase=='TITLES',batch_confirmation_status=phase,current_owner='SYSTEM' if phase=='RUNNING' else 'USER',production_policy='AUTO_PRODUCTION_V1',batch_review=meta,content_confirmation_required=True,scripts_approved=True,titles_approved=phase=='RUNNING')
  for rank,row in updates.items():
   row['action_revision']=row.get('action_revision',0)+1
   db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',(json.dumps(row,ensure_ascii=False),j['id'],rank))
  for rank,stage,value in stages:db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)',(j['id'],stage,rank,digest(value),json.dumps(value,ensure_ascii=False)))
  db.execute('UPDATE jobs SET data=?,state=?,updated=? WHERE id=?',(json.dumps(raw,ensure_ascii=False),'QUEUED' if phase=='RUNNING' else 'WAITING_TITLE_APPROVAL',time.time(),j['id']))
 def scripts(self,id,data):
  with self.store.lock:
   j,b,repeated=self.validate(id,'SCRIPTS',data)
   if repeated:
    if hasattr(self.manager,'resume_job_after_user_action'):self.manager.resume_job_after_user_action(id)
    return dict(replayed=True)
   if not b['all_scripts_ready']:raise ValueError('BATCH_NOT_READY')
   updates={};stages=[];language=target_language(j)
   for x in j['items']:
    if x['rank'] not in b['ranks'] or x.get('batch_script_approved'):continue
    c=content_view(j,x,self.content.translations.cache);d=x.get('script_draft') if c['draft_id'] else None;music=c['voiceover_mode']=='MUSIC_ONLY'
    spoken=dict(text='',text_zh='',language=language) if music else {k:c['script'][k] for k in ('text','text_zh','language')}
    response=copy.deepcopy(x.get('copy') or recovered_copy(self.store,j,x));response['localized_script']=spoken
    # Initial titles were co-generated in the existing Batch call. A replacement
    # supplies new per-item titles; expose them only after the script barrier.
    titles=copy.deepcopy((d or {}).get('titles') or c['titles'] or response.get('titles',[]));response['titles']=titles
    changed=(script(x) or {}).get('text','')!=spoken['text'] or (x.get('voiceover_mode')=='MUSIC_ONLY')!=music or music and d and d.get('music')!=x.get('music')
    row=dict(x,copy=response,**script_fields(response,j,music),final_language_version=VERSION,script_status='USER_APPROVED',batch_script_approved=True,content_confirmed=False,status='WAITING_TITLE_APPROVAL',title_status='WAITING_APPROVAL',title_candidates=titles,content_titles=None,batch_selected_title_id=titles[0].get('id') if titles else None,batch_resume_phase='SCRIPT' if changed else 'TITLE',issues=[])
    if d:
     row.update(approved_draft_id=d['id'],script_source=d['source'],music=d.get('music'),script_revision=x.get('script_revision',0)+int(bool(changed)),content_custom_title=None)
     if d['mode']=='PASTED':row.update(pasted_original_text=d['original_text'],pasted_original_language=d['original_language'])
     if d['mode'] in {'PASTED','TABLE'}:row.update(source_script=d['original_text'],clean_source_script=d['original_text'],source_language=d['original_language'])
    if changed:row.update(audio_result=None,voice=None,decision=None,plan=None,output=None,previous_output=x.get('output') or x.get('previous_output'),duration_match=None,supplement_plan=None)
    updates[x['rank']]=row;stages.extend([(x['rank'],'SCRIPT_APPROVED',spoken),(x['rank'],'FINAL_SCRIPT_LANGUAGE_CHECK',dict(status='EXEMPT_MUSIC_ONLY' if music else 'PASS',language=language))])
   with self.store.connect() as db:
    db.execute('BEGIN IMMEDIATE')
    for rank,row in updates.items():
     invalid=['TITLE_APPROVED','FINAL_RENDERED','QA_DONE','FINAL_APPROVED']+(['TTS_DONE','ALIGNMENT','SUBTITLE'] if row['batch_resume_phase']=='SCRIPT' else [])
     db.execute('DELETE FROM checkpoints WHERE job_id=? AND item IN (?,?) AND stage IN ('+','.join('?' for _ in invalid)+')',(id,rank,str(int(rank)),*invalid))
    self.commit(db,j,b,data,'TITLES',updates,stages)
   self.observe(j,updates,'WAITING_TITLE_APPROVAL')
   if getattr(self.manager,'confirmation_wait',None):self.manager.confirmation_wait.confirmed(id)
   if hasattr(self.manager,'resume_job_after_user_action'):self.manager.resume_job_after_user_action(id)
   return dict(replayed=False)
 def titles(self,id,data):
  with self.store.lock:
   j,b,repeated=self.validate(id,'TITLES',data)
   if repeated:
    if hasattr(self.manager,'resume_job_after_user_action'):self.manager.resume_job_after_user_action(id)
    return dict(replayed=True)
   choices=data.get('selections')
   if not isinstance(choices,dict) or set(choices)!=set(b['ranks']):raise ValueError('BATCH_TITLE_REQUIRED')
   if not b['all_titles_ready']:raise ValueError('BATCH_NOT_READY')
   updates={};stages=[]
   for x in j['items']:
    if x['rank'] not in b['ranks']:continue
    c=content_view(j,x,self.content.translations.cache)
    if not x.get('batch_script_approved') and x.get('script_status')!='USER_APPROVED':raise ValueError('BATCH_NOT_READY')
    if c['draft_id']:raise ValueError('BATCH_NOT_READY')
    if c['voiceover_mode']!='MUSIC_ONLY':check(script(x),target_language(j),require_metadata=True)
    candidates=c['titles']+([c['custom_title']] if c['custom_title'] else []);t=next((t for t in candidates if str(t.get('id'))==str(choices[x['rank']])),None)
    if not valid_title(t,j):raise ValueError('BATCH_TITLE_REQUIRED')
    from .production_preflight import require_title
    require_title(j,x,t['text'])
    snap=lambda t:{k:t[k] for k in ('id','text','text_zh','language','angle') if k in t}
    title=snap(t);response=copy.deepcopy(x['copy']);response['titles']=[snap(t) for t in c['titles']+([c['custom_title']] if title['id']==4 else [])]
    updates[x['rank']]=dict(x,copy=response,title_candidates=response['titles'],content_titles=None,selected_title_id=title['id'],batch_selected_title_id=title['id'],title=title['text'],final_title=title,final_title_language=title['language'],title_text_zh=title['text_zh'],title_status='USER_APPROVED',content_confirmed=True,content_confirmed_at=time.time(),content_edit=False,status='QUEUED',issues=[],review_reason=None,review_required=False,waiting_reason=None,current_owner='SYSTEM',user_action_status='NONE',resume_pending=True,resume_running=False,production_policy='AUTO_PRODUCTION_V1',resume_phase=x.get('batch_resume_phase','TITLE'))
    stages.append((x['rank'],'TITLE_APPROVED',title))
   with self.store.connect() as db:
    db.execute('BEGIN IMMEDIATE');self.commit(db,j,b,data,'RUNNING',updates,stages)
   self.observe(j,updates,'QUEUED')
   if getattr(self.manager,'confirmation_wait',None):self.manager.confirmation_wait.confirmed(id)
   if hasattr(self.manager,'resume_job_after_user_action'):self.manager.resume_job_after_user_action(id)
   else:self.manager.enqueue_batch(id,[(rank,x['resume_phase']) for rank,x in updates.items()])
   return dict(replayed=False)
 def reopen_script(self,id,rank,token):
  with self.store.lock:
   j=self.store.get(id);b=self.view(id)
   if j.get('deleted_at') or b['phase']!='TITLES' or b['action_token']!=token or rank not in b['ranks']:raise ValueError('STALE_ACTION')
   with self.store.connect() as db:
    db.execute('BEGIN IMMEDIATE');raw=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(id,)).fetchone()[0]);meta=raw.get('batch_review') or dict(version=1,ranks=b['ranks'],receipts={});meta['phase']='SCRIPTS';raw.update(script_confirmation_pending=True,title_confirmation_pending=False,batch_confirmation_status='SCRIPTS',current_owner='USER',production_policy='AUTO_PRODUCTION_V1',batch_review=meta,scripts_approved=False,titles_approved=False)
    db.execute('UPDATE jobs SET data=?,state=? WHERE id=?',(json.dumps(raw,ensure_ascii=False),'WAITING_SCRIPT_APPROVAL',id))
    x=next(x for x in j['items'] if x['rank']==rank);x.update(status='WAITING_SCRIPT_APPROVAL',batch_script_approved=False,script_status='WAITING_APPROVAL',content_edit=True,action_revision=x.get('action_revision',0)+1)
    db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',(json.dumps(x,ensure_ascii=False),id,rank))
    db.execute("DELETE FROM checkpoints WHERE job_id=? AND item IN (?,?) AND stage IN ('SCRIPT_APPROVED','TITLE_APPROVED')",(id,rank,str(int(rank))))
