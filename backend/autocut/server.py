from .script_language import target_language,check,script,script_fields,issues as language_issues,VERSION
import fcntl,json,logging,os,secrets as tokens,socket,sys,time,uuid
from pathlib import Path
from threading import Thread
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from pydantic import BaseModel,ConfigDict,Field
from .content_confirmation import ContentConfirmation,ERRORS as CONTENT_ERRORS
from .batch_confirmation import BatchConfirmation,batch_view,ERRORS as BATCH_ERRORS
from .context import RES,HOME,ENGINE,asset
from .storage import Store,product_name
from .job_deletion import JobDeletion
from .item_review import ItemReview,REASONS,review_route,human_reason,get_item
from .review_media import import_media
from .ownership import present_ownership,require_action,item_state,ERRORS as STATE_ERRORS
from .secrets import Keychain
from .drivers import CodexDriver,OpenAIDriver,DriverRouter
from .references import parse
from .pipeline import Manager
from .provider_models import MODEL_OPTIONS,validate_model,job_models
from .task_instructions import InstructionService,safe_raw,digest
from .copy_presentation import TranslationService, TranslationCache, presented_job, job_texts, approval_guard, text_key
from starlette.concurrency import run_in_threadpool
from .workbench import ScriptMode,tags,save_tags,TAG_INTENTS,merge_tags,save_preset,table_scripts,existing_scripts,script_snapshot,approve_scripts

class Strict(BaseModel):model_config=ConfigDict(extra='forbid')
class NewJob(Strict):
 request_id:str=Field(min_length=10,max_length=80)
 input_path:str
 product_profile_id:str='general'
 product_name:str|None=None
 market:str='Brazil'
 mode:str='Transport'
 output_count:int=Field(default=10,ge=1,le=30)
 title_approval_mode:str='auto'
 watermark_mode:str='auto'
 script_reference_file:str|None=None
 script_reference_mode:str='smart'
 script_mode:ScriptMode=ScriptMode.AUTO
 script_reference_id:str|None=None
 existing_script_id:str|None=None
 custom_tag_ids:list[str]=Field(default_factory=list,max_length=30)
 source_names:list[str]|None=None
 custom_instruction_raw:str=Field(default='',max_length=4000)
 instruction_receipt:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
class Setting(Strict):
 ai_driver:str|None=None
 title_approval:str|None=None
 watermark:str|None=None
 open_output:bool|None=None
 deepseek_model:str|None=None
 elevenlabs_model:str|None=None

ERRORS={'AI_DRIVER_UNAVAILABLE':'AI 执行引擎暂时不可用，请在设置中检查连接','OPENAI_NOT_CONFIGURED':'请在设置中配置 OpenAI','DEEPSEEK_NOT_CONFIGURED':'请在设置中配置 DeepSeek','ELEVENLABS_NOT_CONFIGURED':'请在设置中配置 ElevenLabs','OVERLAY_REQUIRES_PREVIEW':'原文字处理效果需要检查','SCRIPT_TABLE_INVALID':'无法读取该文案表格','NO_VALID_SOURCE':'文件夹中没有有效视频','DURATION_NEEDS_SUPPLEMENT_REVIEW':'口播长度与画面不匹配，需要复核','JOB_REVIEW_REQUIRED':'任务遇到问题，请查看技术详情','MATCHED_VOICE_UNAVAILABLE':'当前账号缺少匹配的声音','KEYCHAIN_UNAVAILABLE':'钥匙串暂时不可用'}
ERRORS.update(NO_RELIABLE_SCRIPT='没有可用口播，需检查素材或提供文案参考',COPY_UNAVAILABLE='文案暂时不可用，请查看服务设置',AI_DRIVER_TIMEOUT='AI 判断超时，可从当前进度继续',AI_DRIVER_EXEC_FAILED='AI 执行未完成，可在设置中检查连接或切换引擎',SOURCE_HASH_CHANGED='源文件已变化，请先检查原素材')
ERRORS.update(PRODUCT_NAME_REQUIRED='请填写有效产品名称（最多 100 字，不含路径符号）',REMIX_NEEDS_SOURCES='混剪至少需要两条可用素材',REMIX_PLAN_INVALID='混剪片段方案需要复核',REMIX_DURATION_REVIEW='混剪片段与配音长度不匹配，需要复核')
ERRORS.update(PROVIDER_MODEL_UNSUPPORTED='请选择列表中支持的模型',PROVIDER_MODEL_UNAVAILABLE='所选模型暂不可用，请检查服务账号',PROVIDER_MODEL_LANGUAGE_UNSUPPORTED='所选配音模型不支持目标语言')
ERRORS.update(CUSTOM_INSTRUCTION_INVALID='无法理解部分要求，请调整后重试',CUSTOM_INSTRUCTION_PARTIAL='部分要求未应用',CUSTOM_INSTRUCTION_FORBIDDEN_ACTION='部分要求涉及程序或全局配置，未应用。',CUSTOM_INSTRUCTION_DRIVER_UNAVAILABLE='AI服务不可用，暂时无法解析本任务要求',CUSTOM_INSTRUCTION_PARSE_FAILED='本任务要求未能解析，请修改要求后重试',REFERENCE_NOT_FOUND='未找到指定文案，已改用智能参考',CUSTOM_INSTRUCTION_BINDING_CHANGED='素材编号已变化，请检查当前任务素材',CUSTOM_INSTRUCTION_RANGE_INVALID='冗余片段的剪辑范围需要复核')

ERRORS.update(DURATION_FIT_FAILED='视频时长不足，当前脚本无法在允许速度范围内完成',INTERNAL_PRODUCTION_ERROR='处理失败，请查看技术详情',TITLE_GENERATION_FAILED='目标语言标题生成失败，请检查服务设置')
ERRORS.update(JOB_DELETED='任务已删除，请先恢复',JOB_RUNNING='任务正在运行，请先停止任务',JOB_NOT_DELETED='请先将任务移到已删除',JOB_PURGE_CONFIRMATION_REQUIRED='请确认永久删除任务',JOB_DATA_IN_USE='该任务数据被其它任务或源素材引用，无法永久删除',JOB_PATH_UNSAFE='任务数据路径需要人工检查',JOB_DELETE_FAILED='任务数据暂时无法清理，请重试')

ERRORS.update(ZH_TRANSLATION_REQUIRED='请先生成中文对照，或明确选择仍使用原文继续',ZH_TRANSLATION_MISSING='中文对照生成失败，可重试',SCRIPT_SELECTION_INVALID='请选择可用脚本',SCRIPT_APPROVAL_NOT_READY='脚本暂未进入确认阶段',PRESET_IN_USE='该预设已被任务引用，确认后可删除；历史任务快照会保留')

def create_app(store,keychain,router,manager,token):
 ERRORS.update(REASONS);ERRORS.update(STATE_ERRORS);ERRORS.update(CONTENT_ERRORS);ERRORS.update(BATCH_ERRORS)
 app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None);grants=set();web=RES/'web'
 instructions=InstructionService(store.home,router);translations=TranslationService(store.home,keychain)
 app.state.translations=translations
 review=ItemReview(store,manager,router,translations);app.state.review=review
 content=ContentConfirmation(store,manager,review,translations);app.state.content=content
 batch=BatchConfirmation(store,manager,content);app.state.batch=batch
 @app.on_event('startup')
 def recover_production():
  if isinstance(manager,Manager):manager.recover_jobs()
 deletion=JobDeletion(store,manager);app.state.deletion=deletion
 def instruction_context(data):
  folder=Path(data['input_path']).expanduser().resolve(strict=True)
  if str(folder) not in grants or not folder.is_dir():raise HTTPException(403,'Folder selection required')
  product=store.product(data['product_profile_id'])
  if not product:raise ValueError('INVALID_REQUEST')
  reference={}
  if data.get('script_reference_file'):
   path=str(Path(data['script_reference_file']).resolve())
   if path not in grants:raise HTTPException(403)
   reference=parse(path)
  # Only immutable current-job evidence. No paths, other jobs, config or secrets.
  from app.s3.visual_preset import load_preset
  context=dict(product=product_name(data.get('product_name') or product['product_name']),market=data['market'],mode=data['mode'],preset_id=product['id'],usage_scope=product.get('usage_scope','production'),current_preset=load_preset(),reference=reference)
  binding=digest(dict(context=context,folder=str(folder),source_names=data.get('source_names'),raw=safe_raw(data.get('custom_instruction_raw','')),tags=data.get('custom_tag_ids',[])))
  return context,binding
 def instruction_snapshot(data):
  raw=safe_raw(data.get('custom_instruction_raw',''))
  if not raw and not data.get('custom_tag_ids'):return instructions.resolve('',{},'auto')
  context,binding=instruction_context(data)
  receipt=data.get('instruction_receipt')
  if receipt:
   path=store.home/'jobs/instruction_drafts'/(receipt+'.json')
   try:record=json.loads(path.read_text())
   except (OSError,ValueError):raise ValueError('CUSTOM_INSTRUCTION_INVALID') from None
   if record['binding']!=binding or record.get('sha256')!=digest(record['snapshot']):raise ValueError('CUSTOM_INSTRUCTION_INVALID')
   return record['snapshot']
  return merge_tags(instructions.resolve(raw,context,store.settings().get('ai_driver','auto')),data.get('custom_tag_ids',[]),store,context)
 def public_job(j):
  raw=j
  j=presented_job(j,store.home,job_models(j)['deepseek']);j['can_delete']=deletion.can_delete(j); j.setdefault('script_mode','AUTO');j['provider_models']=job_models(j);j['human_issues']=[ERRORS.get(x,'需要复核：'+x) for x in j.get('issues',[])]
  j['human_issues']=[ERRORS.get(code,'处理失败，请查看技术详情') for code in j.get('issues',[])]
  for x in j['items']:
   x['human_issues']=[ERRORS.get(code,'处理失败，请查看技术详情') for code in x.get('issues',[])];x['review_route']=review_route(x);x['review_message']=human_reason(x)
  j=present_ownership(store,raw,j);j['batch_review_view']=batch_view(store,raw);j['ui_status']=j['batch_review_view']['label']
  b=j['batch_review_view']
  if b['phase'] in {'SCRIPTS','TITLES'} and not raw.get('deleted_at'):
   for x in j['items']:
    row=next(r for r in b['items'] if r['rank']==x['rank'])
    if row['in_scope']:
     ready=row['script_ready'] if b['phase']=='SCRIPTS' else row['title_ready']
     x['ui_status']='待确认' if ready else '处理中' if x['current_owner']=='SYSTEM' else '需要处理'
   j['content_counts']={k:sum(x['ui_status']==label for x in j['items']) for k,label in [('processing','处理中'),('pending','待确认'),('completed','已完成'),('exception','需要处理')]}
  if j['items']:j['progress']=round(sum(x['ui_progress'] for x in j['items'])/len(j['items']))
  b=j['batch_review_view'];j.update(job_status=j['ui_status'],current_stage=b['phase'],owner='USER' if b['allowed_actions'] else 'SYSTEM',batch_state=b['phase'],pending_action=next(iter(b['allowed_actions']),None))
  return j
 @app.middleware('http')
 async def guard(request,call_next):
  host=request.headers.get('host','');origin=request.headers.get('origin')
  if not (host.startswith('127.0.0.1:') or host.startswith('localhost:')):return JSONResponse({'error':'LOCAL_ONLY'},403)
  if origin and origin not in {'http://'+host}:return JSONResponse({'error':'ORIGIN_REJECTED'},403)
  if request.url.path=='/health':return JSONResponse(dict(status='PASS',app='AutoCut',version=json.loads((RES/'version.json').read_text())))
  if request.headers.get('authorization')!='Bearer '+token and request.cookies.get('autocut_session')!=token:return JSONResponse({'error':'AUTH_REQUIRED'},401)
  response=await call_next(request)
  response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff';response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'"
  return response
 @app.exception_handler(KeyError)
 async def missing(request,exc):return JSONResponse({'error':'JOB_NOT_FOUND','message':'任务不存在'},404)
 @app.exception_handler(ValueError)
 async def bad(request,exc):
  code=str(exc) if str(exc) in ERRORS else 'INVALID_REQUEST'
  return JSONResponse(dict(error=code,message=ERRORS.get(code,'请检查输入')),409 if code in {'STALE_ACTION','STATE_TRANSITION_REJECTED','FINAL_NOT_READY'} else 400)
 @app.get('/')
 def index():return FileResponse(web/'index.html')
 @app.get('/static/{name}')
 def static(name:str):
  if name not in {'app.js','review.js','content.js','workspace.js','app.css','autocut-mark.svg'}:raise HTTPException(404)
  return FileResponse(web/name)
 @app.get('/jobs')
 def jobs(deleted:bool=False):return [public_job(j) for j in store.jobs(deleted=deleted)]
 @app.post('/jobs/{id}/delete')
 def delete_job(id:str):
  return public_job(deletion.soft_delete(id))
 @app.post('/jobs/{id}/restore')
 def restore_job(id:str):
  return public_job(deletion.restore(id))
 @app.post('/jobs/{id}/purge')
 async def purge_job(id:str,request:Request):
  data=await request.json()
  if data!={'confirmed':True}:raise ValueError('JOB_PURGE_CONFIRMATION_REQUIRED')
  return await run_in_threadpool(deletion.purge,id)
 @app.get('/job-name')
 def job_name(product_name:str):return dict(store.next_name(product_name),provisional=True)
 @app.get('/jobs/{id}')
 def job(id:str):
  try:
   if isinstance(manager,Manager):manager.resume_job_after_user_action(id,True)
   return public_job(store.get(id))
  except KeyError:raise HTTPException(404)
 @app.post('/native/grant')
 async def grant(request:Request):
  d=await request.json();p=Path(d['path']).expanduser().resolve(strict=True);grants.add(str(p));return dict(path=str(p),display_path=str(p).replace(str(Path.home()),'~',1),video_count=sum(f.is_file() and not f.name.startswith('.') and f.suffix.lower() in {'.mp4','.mov','.m4v','.webm'} for f in p.iterdir()) if p.is_dir() else None)
 @app.post('/references/inspect')
 async def reference(request:Request):
  d=await request.json();p=str(Path(d['path']).resolve())
  if p not in grants:raise HTTPException(403)
  result=parse(p);result['scripts']={m:[translations.cache.row(r,store.settings()['deepseek_model']) for r in table_scripts(result,m)] for m in ['Brazil','Mexico']};return result
 @app.post('/jobs')
 def new_job(spec:NewJob):
  data=spec.model_dump();folder=Path(data['input_path']).expanduser().resolve(strict=True)
  if str(folder) not in grants or not folder.is_dir():raise HTTPException(403,'Folder selection required')
  existing=store.by_request(data['request_id'])
  if existing:return public_job(existing)
  product=store.product(data['product_profile_id'])
  if not product:raise ValueError('INVALID_REQUEST')
  if data['market'] not in {'Brazil','Mexico'} or data['mode'] not in {'Transport','Remix'} or data['title_approval_mode'] not in {'auto','manual'} or data['watermark_mode'] not in {'auto','handled'} or data['script_reference_mode'] not in {'smart','prefer_original'}:raise ValueError('INVALID_REQUEST')
  name=product_name(data['product_name'] if data['product_name'] is not None else product['product_name'] if product['id']!='general' else '')
  data.update(input_path=str(folder),product_name=name,product_profile=dict(product,product_name=name))
  data['target_language']=target_language(data)
  if data['mode']=='Transport':data['output_count']=None
  if data['script_reference_file']:
   if str(Path(data['script_reference_file']).resolve()) not in grants:raise HTTPException(403)
   data['reference']=parse(data['script_reference_file'])
  data=script_snapshot(data,store)
  snapshot=instruction_snapshot(data);data.pop('instruction_receipt',None);data.update(snapshot)
  override=snapshot['resolved_overrides_json']
  if data['mode']=='Remix' and override.get('output_count'):data['output_count']=override['output_count']
  # Job creation is idempotent. Browser refresh never creates another task.
  data['content_confirmation_required']=True;data['batch_review_required']=True
  j,created=store.create(data)
  if created:
   if data['script_reference_file']:
    import shutil
    destination=store.home/'jobs'/j['id']/'references'/Path(data['script_reference_file']).name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(data['script_reference_file'],destination);store.update(j['id'],script_reference_file=str(destination))
   manager.enqueue(j['id'])
  return public_job(store.get(j['id']))
 @app.post('/instructions/resolve')
 def resolve_instruction(spec:NewJob):
  data=spec.model_dump();context,binding=instruction_context(data)
  snapshot=instruction_snapshot(data)
  receipt=uuid.uuid4().hex;folder=store.home/'jobs/instruction_drafts';folder.mkdir(parents=True,exist_ok=True)
  (folder/(receipt+'.json')).write_text(json.dumps(dict(binding=binding,snapshot=snapshot,sha256=digest(snapshot)),ensure_ascii=False))
  return dict(receipt=receipt,summary=snapshot['instruction_summary'],status=snapshot['custom_instruction_status'],issues=snapshot['instruction_issues'],warning='部分要求涉及程序或全局配置，未应用。' if 'CUSTOM_INSTRUCTION_FORBIDDEN_ACTION' in snapshot['instruction_issues'] else '部分要求未应用，已按安全范围处理。' if snapshot['instruction_issues'] else '')
 def validate_tokens(j,data,action):
  selected=data.get('selections',{});tokens=data.get('action_tokens',{})
  rows=[x for x in j['items'] if action in item_state(store,j,x)['allowed_actions'] and (not selected or x['rank'] in selected)]
  if not rows:raise ValueError('STALE_ACTION')
  for x in rows:
   if tokens.get(x['rank']) is None:raise ValueError('STALE_ACTION')
   require_action(store,j['id'],x['rank'],action,tokens[x['rank']])
  return rows
 def save_title(j,x,data):
  from .copy_presentation import chinese
  t=next((t for t in x.get('title_candidates',[]) if str(t['id'])==str(data.get('candidate',x.get('selected_title_id')))),None)
  if not t:raise ValueError('ITEM_TITLE_INVALID')
  check(t,target_language(j),'TITLE')
  if not chinese(t.get('text_zh')) and data.get('allow_original') is not True:raise ValueError('ZH_TRANSLATION_REQUIRED')
  if not chinese(t.get('text_zh')):store.update(j['id'],title_zh_bypass=True)
  store.done(j['id'],'TITLE_APPROVED',x['rank'],digest(t),t)
  store.item(j['id'],x['rank'],selected_title_id=t['id'],title=t['text'],final_title=t,final_title_language=target_language(j),title_text_zh=t.get('text_zh',''),title_status='USER_APPROVED',status='COPY_READY',issues=[])
  manager.enqueue_item(j['id'],x['rank'],'TITLE')
 @app.get('/jobs/{id}/content-review')
 def batch_content_review(id:str):return public_job(store.get(id))
 @app.post('/jobs/{id}/batch/{action}')
 async def batch_action(id:str,action:str,request:Request):
  data=await request.json()
  with deletion.edit(id):
   if action=='approve-scripts':result=await run_in_threadpool(batch.scripts,id,data)
   elif action=='approve-titles':result=await run_in_threadpool(batch.titles,id,data)
   elif action=='reopen-script':
    await run_in_threadpool(batch.reopen_script,id,str(data.get('rank','')),data.get('action_token'));result={}
   else:raise HTTPException(404)
   return dict(public_job(store.get(id)),batch_result=result)
 @app.post('/jobs/{id}/approve-scripts')
 async def approve_script_route(id:str,request:Request):
  data=await request.json()
  with deletion.edit(id),store.lock:
   j=store.get(id);rows=validate_tokens(j,data,'APPROVE_SCRIPT')
   for x in rows:
    if x.get('script_draft') and x.get('approved_draft_id')!=x['script_draft']['id']:raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
   choices=approve_scripts(store,id,data.get('selections',{}),data.get('allow_original',False),ranks=[x['rank'] for x in rows])
   for rank in choices:manager.enqueue_item(id,rank,'SCRIPT')
   return dict(status='QUEUED')
 @app.post('/jobs/{id}/approve-titles')
 async def approve(id:str,request:Request):
  data=await request.json()
  with deletion.edit(id),store.lock:
   j=store.get(id);rows=validate_tokens(j,data,'APPROVE_TITLE')
   for x in rows:save_title(j,x,dict(candidate=data.get('selections',{}).get(x['rank'],x.get('selected_title_id')),allow_original=data.get('allow_original')))
   return dict(status='QUEUED')
 @app.post('/jobs/{id}/retry')
 async def retry(id:str,request:Request):
  with deletion.edit(id):
   j=store.get(id)
   d=await request.json()
   if d.get('repair')=='CTA_REPAIR':
    from .cta_repair import request as repair_cta
    await run_in_threadpool(repair_cta,manager,id,d.get('ranks') or [d.get('rank')])
    return public_job(store.get(id))
   if j['state'] in {'RUNNING','STOPPING'}:raise HTTPException(409)
   if d.get('repair')=='TITLE_BLUR_WHITE_COVER':
    from .title_blur_cover import request_repair
    request_repair(manager,id,d.get('rank'),d.get('region'));return public_job(store.get(id))
   if d.get('repair')=='VISUAL_SEVERITY_V1':
    from .production_resume import recheck_visual_severity
    recheck_visual_severity(manager,id,d.get('ranks'));return public_job(store.get(id))
   if d.get('retain_original'):store.update(id,retain_original=True)
   manager.enqueue(id);return dict(status='QUEUED')
 @app.post('/jobs/{id}/stop')
 def stop(id:str):
  with deletion.edit(id):
   manager.stop(id);return dict(status='STOPPING')
 @app.post('/jobs/{id}/abandon')
 def abandon(id:str):
  with deletion.edit(id):
   if store.get(id)['state'] in {'RUNNING','STOPPING'}:raise HTTPException(409)
   store.update(id,state='ABANDONED');return dict(status='ABANDONED')
 @app.post('/jobs/{id}/items/{rank}/approve-final')
 @app.post('/jobs/{id}/items/{rank}/review/accept')
 async def item_accept(id:str,rank:str,request:Request):
  data=await request.json()
  if not data.get('action_token'):raise ValueError('STALE_ACTION')
  with deletion.edit(id):return review.accept(id,rank,data.get('action_token'))
 @app.post('/jobs/{id}/items/{rank}/approve-overlay')
 async def item_overlay(id:str,rank:str,request:Request):
  data=await request.json()
  if not data.get('action_token'):raise ValueError('STALE_ACTION')
  with deletion.edit(id):return review.approve_overlay(id,rank,data.get('action_token'))
 @app.post('/jobs/{id}/items/{rank}/review/continue')
 async def item_continue(id:str,rank:str,request:Request):
  data=await request.json()
  with deletion.edit(id):
   with store.lock:
    j,x=review.editable(id,rank);state=item_state(store,j,x)
    action=data.get('action') or next((a for a in state['allowed_actions'] if a in {'REPAIR_LOCALIZATION','MODIFY_OVERLAY','MODIFY_FINAL','RESUME_PROCESSING','RETRY_PROCESSING'}),None)
    require_action(store,id,rank,action,data.get('action_token'))
    route={'REPAIR_LOCALIZATION':'LOCALIZATION','MODIFY_OVERLAY':'VISUAL','MODIFY_FINAL':'VISUAL','RESUME_PROCESSING':'RESUME','RETRY_PROCESSING':'RESUME'}.get(action)
    if not route:raise ValueError('STALE_ACTION')
    if route in {'VISUAL','RESUME'}:manager.enqueue_item(id,rank,x.get('resume_phase','SCRIPT') if route=='RESUME' else 'VISUAL')
   if route=='LOCALIZATION':
    draft=await run_in_threadpool(review.draft,id,rank,dict(mode='LOCALIZATION',confirmed_rework=True,action_token=data.get('action_token')))
    return dict(route=route,draft=draft)
   return dict(route=route)
 @app.post('/jobs/{id}/items/{rank}/script/draft')
 async def item_draft(id:str,rank:str,request:Request):
  with deletion.edit(id):
   data=await request.json();j,x=review.editable(id,rank)
   if data.get('mode')=='TABLE':
    path=data.pop('reference_path',None)
    if path:
     path=str(Path(path).resolve())
     if path not in grants:raise HTTPException(403)
     data['reference']=parse(path)
    else:data['reference']=j.get('reference',{})
   if data.get('mode')=='MUSIC_ONLY':
    path=str(Path(data.pop('music_path','')).resolve())
    if path not in grants:raise HTTPException(403)
    data['music']=await run_in_threadpool(import_media,store.home,id,path,'audio')
   else:data.pop('music',None)
   return await run_in_threadpool(review.draft,id,rank,data)
 @app.post('/jobs/{id}/items/{rank}/approve-script')
 @app.post('/jobs/{id}/items/{rank}/script/approve')
 async def item_script_approve(id:str,rank:str,request:Request):
  with deletion.edit(id):
   data=await request.json()
   if not data.get('action_token'):raise ValueError('STALE_ACTION')
   return await run_in_threadpool(review.approve,id,rank,data.get('draft_id'),data.get('candidate'),data.get('action_token'))
 @app.get('/jobs/{id}/items/{rank}/scripts')
 def item_scripts(id:str,rank:str):
  j,x=get_item(store,id,rank);ref=x.get('script_draft',{}).get('reference_snapshot') or j.get('reference',{})
  return dict(filename=ref.get('filename','文案表'),rows=[translations.cache.row(r,job_models(j)['deepseek']) for r in table_scripts(ref,j['market'])])
 @app.post('/jobs/{id}/items/{rank}/scripts/translate')
 async def item_scripts_translate(id:str,rank:str,request:Request):
  with deletion.edit(id):
   data=await request.json();j,x=review.editable(id,rank);ids=data.get('ids',[])
   if not isinstance(ids,list) or not 1<=len(ids)<=20:raise ValueError('INVALID_REQUEST')
   path=data.get('reference_path')
   if path:
    path=str(Path(path).resolve())
    if path not in grants:raise HTTPException(403)
    ref=parse(path)
   else:ref=j.get('reference',{})
   rows=[r for r in table_scripts(ref,j['market']) if r['id'] in ids]
   return await run_in_threadpool(translations.translate,rows,job_models(j)['deepseek'])
 @app.post('/jobs/{id}/items/{rank}/content/{action}')
 async def content_action(id:str,rank:str,action:str,request:Request):
  data=await request.json()
  if not data.get('action_token'):raise ValueError('STALE_ACTION')
  with deletion.edit(id):
   if action=='begin':await run_in_threadpool(content.begin,id,rank,data['action_token'])
   elif action=='approve':await run_in_threadpool(content.approve,id,rank,data)
   elif action in {'titles','custom-title'}:await run_in_threadpool(content.titles,id,rank,data['action_token'],data.get('text','') if action=='custom-title' else None)
   else:raise HTTPException(404)
   return public_job(store.get(id))
 @app.post('/jobs/{id}/items/{rank}/approve-title')
 @app.post('/jobs/{id}/items/{rank}/title/approve')
 async def item_title_approve(id:str,rank:str,request:Request):
  data=await request.json()
  if not data.get('action_token'):raise ValueError('STALE_ACTION')
  with deletion.edit(id),store.lock:
   j,x,state=require_action(store,id,rank,'APPROVE_TITLE',data.get('action_token'))
   save_title(j,x,data);return dict(status='QUEUED')
 @app.post('/jobs/{id}/items/{rank}/title/draft')
 async def item_title_draft(id:str,rank:str,request:Request):
  data=await request.json()
  with deletion.edit(id):
   with store.lock:
    j,x,state=require_action(store,id,rank,'REGENERATE_TITLE',data.get('action_token'))
    store.item(id,rank,status='SCRIPTING',operation_inflight=True)
   try:
    source,analysis,_=await run_in_threadpool(review.evidence,id,rank)
    made=await run_in_threadpool(review.copy_request,dict(mode='TITLE',input_text=x.get('copy',{}).get('localized_script',{}).get('text',''),product=j.get('product_profile'),video_analysis=analysis,target_language=target_language(j)),job_models(j)['deepseek'])
    titles=[dict(t,id=n) for n,t in enumerate(made['titles'],1)];copy_data=dict(x.get('copy',{}),titles=titles)
    store.item(id,rank,title_candidates=titles,copy=copy_data,status='WAITING_TITLE_APPROVAL',operation_inflight=False)
   except Exception:
    store.item(id,rank,status=x['status'],operation_inflight=False);raise
   return public_job(store.get(id))
 @app.post('/jobs/{id}/items/{rank}/supplements')
 async def item_supplements(id:str,rank:str,request:Request):
  with deletion.edit(id):
   data=await request.json();require_action(store,id,rank,'ADD_SUPPLEMENT_MEDIA',data.get('action_token'));paths=data.get('paths',[]);urls=data.get('urls',[])
   if not isinstance(paths,list) or not isinstance(urls,list) or not 1<=len(paths)+len(urls)<=5 or not all(isinstance(v,str) and len(v)<=2000 for v in paths+urls):raise ValueError('INVALID_REQUEST')
   paths=[str(Path(p).resolve()) for p in paths]
   if any(p not in grants for p in paths):raise HTTPException(403)
   return await run_in_threadpool(review.add_media,id,rank,paths,urls,data.get('action_token'))
 @app.post('/jobs/{id}/items/{rank}/supplements/reuse')
 async def item_supplements_reuse(id:str,rank:str,request:Request):
  data=await request.json()
  with deletion.edit(id):
   await run_in_threadpool(review.reuse_media,id,rank,data.get('action_token'));return public_job(store.get(id))
 @app.get('/outputs')
 def outputs():return store.outputs()
 @app.get('/outputs/{jobid}/{rank}/video')
 def video(jobid:str,rank:str):
  row=next((x for x in store.get(jobid)['items'] if x['rank']==rank),None)
  if not row or not row.get('output'):raise HTTPException(404)
  return FileResponse(row['output'],media_type='video/mp4')
 @app.get('/jobs/{jobid}/items/{rank}/source')
 def source_video(jobid:str,rank:str):
  row=next((x for x in store.get(jobid)['items'] if x['rank']==rank),None)
  if not row or not row.get('source_path') or not Path(row['source_path']).is_file():raise HTTPException(404)
  return FileResponse(row['source_path'],media_type='video/mp4')
 @app.get('/jobs/{jobid}/items/{rank}/preview')
 def overlay_preview(jobid:str,rank:str):
  j,x=get_item(store,jobid,rank);path=x.get('overlay_preview')
  if not path or not Path(path).is_file():raise HTTPException(404)
  return FileResponse(path,media_type='video/mp4')
 @app.get('/outputs/{jobid}/{rank}/thumbnail')
 def thumb(jobid:str,rank:str):
  row=next((x for x in store.get(jobid)['items'] if x['rank']==rank),None)
  if not row or not (row.get('thumbnail') or row.get('source_sheet')):raise HTTPException(404)
  # Reuse the first frame from the existing QA sheet; UI never decodes a video.
  from PIL import Image
  from io import BytesIO
  try:
   with Image.open(row.get('thumbnail') or row['source_sheet']) as im:
    if not row.get('thumbnail'):
     data=BytesIO();im.crop((0,0,240,426)).convert('RGB').save(data,format='JPEG',quality=85);return Response(data.getvalue(),media_type='image/jpeg')
    if im.size==(810,1530):
     data=BytesIO();im.crop((0,0,270,480)).convert('RGB').save(data,format='JPEG',quality=90)
     return Response(data.getvalue(),media_type='image/jpeg')
  except (OSError,ValueError):raise HTTPException(404)
  return FileResponse(row['thumbnail'])
 @app.get('/products')
 def products():return store.products()
 @app.post('/products')
 async def product(request:Request):
  return save_preset(store,await request.json())
 @app.post('/products/{id}/delete')
 async def delete_product(id:str,request:Request):
  if id=='general':raise ValueError('INVALID_REQUEST')
  data=await request.json()
  if any(j.get('product_profile_id')==id for j in store.jobs(deleted=None)) and not data.get('confirmed'):raise ValueError('PRESET_IN_USE')
  with store.lock,store.connect() as db:
   deleted=store.settings().get('deleted_preset_ids',[])
   db.execute('DELETE FROM product_profiles WHERE id=?',(id,))
   db.execute('INSERT OR REPLACE INTO app_settings VALUES(?,?)',('deleted_preset_ids',json.dumps(list(dict.fromkeys(deleted+[id])))))
  return dict(status='DELETED')
 @app.get('/scripts')
 def scripts(product:str='',market:str='Brazil',preset_id:str='general'):
  return [translations.cache.row(r,store.settings()['deepseek_model']) for r in existing_scripts(store,product,market,preset_id)]
 @app.post('/copy/translate')
 async def translate_selection(request:Request):
  d=await request.json();scope=d.get('scope');model=store.settings()['deepseek_model']
  if scope=='table':
   path=str(Path(d['path']).resolve())
   if path not in grants:raise HTTPException(403)
   rows=table_scripts(parse(path),d.get('market','Brazil'))
  elif scope=='existing':rows=existing_scripts(store,d.get('product',''),d.get('market','Brazil'),d.get('preset_id'))
  else:raise ValueError('INVALID_REQUEST')
  ids=d.get('ids',[])
  if not isinstance(ids,list) or not 1<=len(ids)<=20 or not all(isinstance(i,str) for i in ids):raise ValueError('INVALID_REQUEST')
  rows=[r for r in rows if r['id'] in ids]
  if len(rows)!=len(set(ids)):raise ValueError('SCRIPT_SELECTION_INVALID')
  return await run_in_threadpool(translations.translate,rows,model)
 @app.post('/jobs/{id}/translations')
 async def translate_job(id:str,request:Request):
  with deletion.edit(id):
   d=await request.json();j=store.get(id);model=job_models(j)['deepseek']
   if d.get('kind') not in (None,'script','title'):raise ValueError('INVALID_REQUEST')
   rows=job_texts(public_job(j),d.get('rank'),d.get('kind'))
   # A click covers at most 20 missing texts. Subsequent clicks continue the rest.
   pending=[r for r in rows if not r.get('text_zh')];unique={}
   for r in pending:unique.setdefault(r['id'],r)
   result=await run_in_threadpool(translations.translate,list(unique.values())[:20],model)
   translated={r['id']:r['text_zh'] for r in result['rows'] if r.get('text_zh')}
   with store.lock:
    latest=store.get(id)
    for item in latest['items']:
     metadata=dict(item.get('presentation_zh',{}))
     for r in rows:
      if r['rank']==item['rank'] and r['id'] in translated:metadata[r['id']]=translated[r['id']]
     if metadata!=item.get('presentation_zh',{}):store.item(id,item['rank'],presentation_zh=metadata)
   return dict(job=public_job(store.get(id)),http_requests=result['http_requests'],warning=result['warning'])
 @app.get('/custom-tags')
 def get_tags():return dict(tags=tags(store),intents=[dict(id=k,label=v[0]) for k,v in TAG_INTENTS.items()])
 @app.post('/custom-tags')
 async def put_tags(request:Request):return dict(tags=save_tags(store,(await request.json())['tags']))
 @app.get('/settings/status')
 def settings():
  providers={}
  for k in ['OPENAI_API_KEY','DEEPSEEK_API_KEY','ELEVENLABS_API_KEY']:
   try:providers[k]=keychain.configured(k)
   except ValueError:providers[k]=False
  from .system_check import check
  engines=check(RES)
  return dict(settings=store.settings(),providers=providers,model_options=MODEL_OPTIONS,drivers=router.health(),engines=engines,version=json.loads((RES/'version.json').read_text()))
 @app.post('/settings')
 def save_settings(spec:Setting):
  values=spec.model_dump(exclude_none=True)
  for k,v in values.items():
   allowed={'ai_driver':{'auto','codex','openai_api'},'title_approval':{'auto','manual'},'watermark':{'auto','handled'}}
   if k in allowed and v not in allowed[k]:raise ValueError('INVALID_REQUEST')
   if k in {'deepseek_model','elevenlabs_model'}:validate_model(k.removesuffix('_model'),v)
  store.set_settings(values)
  return dict(status='SAVED')
 @app.post('/settings/providers/{provider}')
 async def secret(provider:str,request:Request):
  names={'openai':'OPENAI_API_KEY','deepseek':'DEEPSEEK_API_KEY','elevenlabs':'ELEVENLABS_API_KEY'}
  if provider not in names:raise HTTPException(404)
  d=await request.json()
  if d.get('clear'):keychain.delete(names[provider])
  else:
   value=d.get('key','').strip()
   if not 8<=len(value)<=500:raise ValueError('INVALID_REQUEST')
   keychain.set(names[provider],value)
  return dict(configured=keychain.configured(names[provider]))
 @app.get('/runtime/active')
 def active():return dict(count=sum(j['state'] in {'RUNNING','QUEUED','STOPPING'} for j in store.jobs()))
 @app.post('/runtime/completions')
 def completions():
  # Native shell consumes once; completion state persists across window refresh/restart.
  events=[]
  with store.lock:
   for j in store.jobs():
    if j['state'] not in {'COMPLETED','REVIEW_REQUIRED'} or j.get('completion_notified'):continue
    folder=(Path(j['input_path'])/'outputs').resolve()
    outputs=[x for x in j['items'] if x.get('output') and Path(x['output']).is_file() and Path(x['output']).resolve().parent==folder]
    if not outputs:continue
    events.append(dict(job_id=j['id'],output_dir=str(folder),open_output=store.settings().get('open_output',True)))
    store.update(j['id'],completion_notified=True)
  return dict(events=events)
 @app.post('/runtime/stop-all')
 def stop_all():
  for j in store.jobs():
   if j['state'] in {'RUNNING','QUEUED','STOPPING'}:manager.stop(j['id'])
  return dict(status='STOPPING')
 @app.post('/runtime/shutdown')
 def shutdown():
  if active()['count']:raise HTTPException(409)
  app.state.server.should_exit=True;return dict(status='STOPPING')
 return app

def main():
 HOME.mkdir(parents=True,exist_ok=True);os.chmod(HOME,0o700)
 lock=(HOME/'runtime/backend.lock');lock.parent.mkdir(exist_ok=True);handle=lock.open('a')
 try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:return
 store=Store(HOME);keychain=Keychain(RES/'bin/autocut-keychain');router=DriverRouter(CodexDriver(HOME/'runtime/judgments'),OpenAIDriver(keychain));manager=Manager(store,keychain,router);token=tokens.token_urlsafe(36)
 asset();app=create_app(store,keychain,router,manager,token)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 state=HOME/'runtime/backend.json';state.write_text(json.dumps(dict(pid=os.getpid(),port=port,token=token,resources=str(RES))));state.chmod(0o600)
 import uvicorn
 server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',access_log=False));app.state.server=server
 try:server.run(sockets=[sock])
 finally:state.unlink(missing_ok=True)
if __name__=='__main__':main()
