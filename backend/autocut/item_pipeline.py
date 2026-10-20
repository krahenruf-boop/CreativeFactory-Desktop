"""Resume one item using the existing plan builder, renderer and QA pipeline."""
import copy,math,re,time
from pathlib import Path
from .file_storage import output_directory
from .copy_presentation import production_copy,digest
from .script_language import target_language,check,script,script_fields,VERSION
from .provider_models import job_models,tts_payload
from .task_instructions import effective,effective_sources,decision_override
from .item_review import get_item,audit,ItemReview
from .review_media import resolve_supplements,duration_check,music_audio
from app.s3.headless_runtime import Harness,read,write,sha,fingerprint
from app.s3.headless_jobs import tts_job
from app.s3.visual_preset import load_preset
from app.s2.providers import Official

def resume_item(manager,id,rank,phase):
 if phase=='AI_RENDER_QA':
  from .ai_recovery import resume_render
  return resume_render(manager,id,f'{int(rank):02}')
 if phase=='AI_FINAL_QA':
  from .ai_recovery import resume_final
  return resume_final(manager,id,f'{int(rank):02}')
 if phase=='CTA_TEXT_LAYERS':
  # The old guard failed before export. Keep the approved plan, timings and
  # audio intact; the corrected renderer composites CTA beneath the text.
  rank=f'{int(rank):02}';job,item=get_item(manager.store,id,rank)
  work=manager.store.home/'jobs'/id/'work';plan=read(item['plan'])
  manager.check(id)
  if Path(plan['output']).exists():raise ValueError('FINAL_OUTPUT_ALREADY_EXISTS')
  manager.store.item(id,rank,status='EDITING',issues=[])
  with Harness(work,render_workers=1) as harness:
   manager.finish_plans(id,harness,job,[plan],work)
  audit(manager.store,id,rank,'CTA_TEXT_LAYERS_RESUMED',status=get_item(manager.store,id,rank)[1]['status'],output=plan['output'])
  return
 if phase=='CTA_REPAIR':
  from .cta_repair import resume
  return resume(manager,id,f'{int(rank):02}')
 if phase=='TITLE_BLUR_WHITE_COVER':
  from .title_blur_cover import resume_repair
  return resume_repair(manager,id,f'{int(rank):02}')
 from .pipeline import make_plan,Stopped
 from .copy_presentation import TranslationService
 store=manager.store;home=store.home;rank=f'{int(rank):02}';j,x=get_item(store,id,rank)
 if j.get('deleted_at') or x['status']=='PASS' and phase not in {'SCRIPT','LOCALIZATION'}:return
 work=home/'jobs'/id/'work';models=job_models(j);service=ItemReview(store,manager,manager.router,TranslationService(home,manager.secrets))
 try:
  if phase=='SUPPLEMENT' and not all(x.get(k) for k in ('source_snapshot','analysis','decision','copy','audio_result')):raise ValueError('SUPPLEMENT_RECOVERY_CACHE_MISSING')
  manager.check(id);store.item(id,rank,status='EDITING' if phase=='SUPPLEMENT' else 'SCRIPTING');store.update(id,state='RUNNING',stage='剪辑' if phase=='SUPPLEMENT' else '文案');source,analysis,sheet=service.evidence(id,rank)
  j,x=get_item(store,id,rank);task=effective(j,rank);music=x.get('voiceover_mode')=='MUSIC_ONLY';response=x.get('copy')
  if not response:raise ValueError('NO_RELIABLE_SCRIPT')
  if j.get('batch_review_required') and (j.get('batch_review') or {}).get('phase')!='RUNNING':raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
  if j.get('content_confirmation_required') and not x.get('content_confirmed'):raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
  if x.get('script_draft') and x.get('approved_draft_id')!=x['script_draft']['id']:raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
  if j.get('script_mode')=='REQUIRE_APPROVAL' and x.get('script_status')!='USER_APPROVED' and not j.get('scripts_approved'):raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
  language=target_language(j)
  if not music:
   spoken=check(script(x),language);response=copy.deepcopy(response);response['localized_script']=spoken
   store.item(id,rank,final_language_version=VERSION,**script_fields(response,j))
  product=j.get('product_profile') or store.product(j['product_profile_id'])
  if not response.get('titles'):
   made=service.copy_request(dict(mode='PASTED',input_text=response['localized_script']['text'] or analysis.get('context',''),target_language=language,input_language=language,product=product),models['deepseek'])
   response['titles']=[dict(t,id=n) for n,t in enumerate(made['titles'],1)];store.item(id,rank,copy=response,title_candidates=response['titles'])
  with Harness(work,render_workers=2) as h:
   decision=x.get('decision')
   if not decision:
    decision=manager.judge(id,h,'EDIT_PLAN',rank,dict(analysis=analysis,voiceover_mode='MUSIC_ONLY' if music else 'VOICEOVER',copy=production_copy(response),duration=source['duration'],language=language,visual_preset=load_preset(),task_overrides=task),'build_edit_plan',[sheet] if sheet else [])
    if not decision['copy_approved'] and not (x.get('script_status')=='USER_APPROVED' and x.get('content_confirmed')):raise ValueError('COPY_REVIEW_REQUIRED')
    decision=decision_override(decision,task);store.item(id,rank,decision=decision)
   # Ordinary source-overlay concerns are evidence, never a human barrier.
   overlay_ok=bool(analysis.get('overlays_need_review'))
   if overlay_ok:store.item(id,rank,qa_warnings=['OVERLAY_REQUIRES_PREVIEW'])
   tid=x.get('selected_title_id') or decision['selected_title_id'];title=next((t for t in response['titles'] if t['id']==tid),response['titles'][0])
   if task.get('title_enabled',True):title=check(title,language,'TITLE')
   from .production_preflight import require_title,before_tts,select_voice
   require_title(j,x,title['text'])
   if j.get('title_approval_mode')=='manual' and not (j.get('titles_approved') or x.get('title_status')=='USER_APPROVED'):
    store.item(id,rank,status='WAITING_TITLE_APPROVAL',title_candidates=response['titles'],selected_title_id=title['id']);return
   if not music:check(script(x),language)
   clips=resolve_supplements(source,decision,task,x.get('supplement_plan'),(x.get('supplement_manifest') or {}).get('media') or j.get('supplement_media',[]),response['localized_script']['text'])
   audio=x.get('audio_result')
   if audio and not music and (not Path(audio.get('path','')).is_file() or sha(audio['path'])!=audio.get('sha256')):raise ValueError('TTS_CACHE_MISSING')
   if music:
    audio=music_audio(x['music'],math.floor(sum(c['source_end']-c['source_start'] for c in clips)*30)/30,work)
   elif not audio or not Path(audio.get('path','')).is_file() or sha(audio['path'])!=audio.get('sha256'):
    if phase=='SUPPLEMENT':raise ValueError('TTS_CACHE_MISSING')
    cat=manager.catalog(models['elevenlabs']);voice=select_voice(product,analysis,cat)
    if not voice:raise ValueError('MATCHED_VOICE_UNAVAILABLE')
    payload,_=tts_payload(response['localized_script']['text'],voice['id'],models['elevenlabs'],cat,language)
    from .production_preflight import receipts
    prior=receipts(home)
    if not before_tts(store,store.get(id),x,payload,cat['scope'],prior,clips):return
    review=dict(title_status='USER_APPROVED',script_status='CODEX_APPROVED',reviewer='Creative Factory',approval_mode='MANUAL',tts_text_sha256=fingerprint(payload['tts_text']))
    manager.check(id);store.item(id,rank,status='VOICE',voice=voice['name']);store.update(id,stage='配音')
    audio=tts_job(h,payload,scope=cat['scope'],provider_factory=lambda:Official('elevenlabs',manager.secrets.get('ELEVENLABS_API_KEY')),review=review,existing_results=prior,execute=True).result()
    if audio.get('error') or audio.get('technical_status')!='PASS':raise ValueError(audio.get('error','VOICE_QA_FAILED'))
    write(home/'cache/tts'/f'{audio["fingerprint"]}.result.json',audio)
   if not music:audio=dict(audio,script_text=response['localized_script']['text'],script_language=language)
   store.item(id,rank,audio_result=audio);store.done(id,'TTS_DONE',rank,audio.get('fingerprint') or digest(audio),audio)
   store.item(id,rank,status='DURATION_FIT');store.update(id,stage='速度适配')
   fit_task=dict(task,conservative_motion=bool(analysis.get('talking_head') or analysis.get('detailed_demonstration')))
   from .auto_supplement import prepare as prepare_append,adapt as adapt_append
   clips,match,append=prepare_append(store.get(id),source,clips,audio['duration'],fit_task,music_only=music)
   store.item(id,rank,duration_match=match,auto_supplement=append)
   if append:store.done(id,'AUTO_SUPPLEMENT_PLAN',rank,digest(append),append)
   store.done(id,'DURATION_FIT',rank,digest([clips,audio['duration'],fit_task]),match)
   if not match['fits']:
    store.item(id,rank,status='WAITING_SUPPLEMENT_MEDIA',issues=['DURATION_FIT_FAILED']);audit(store,id,rank,'DURATION_FIT_FAILED',**match);return
   audit(store,id,rank,'DURATION_AUTO_FIT',**match)
   adapted=copy.deepcopy(source)
   if x.get('supplement_plan'):
    adapted.update(remix_clips=clips,duration=sum(c['source_end']-c['source_start'] for c in clips),primary_source=source.get('primary_source',source),creative_angle='用户补充同产品画面')
   adapted=adapt_append(adapted,clips,append)
   from .source_library import adapt as adapt_health
   adapted=adapt_health(adapted,clips)
   # A new item revision never overwrites an already exported MP4.
   attempt=x.get('render_revision',0)+1;output=output_directory(j)/f'AutoCut_{id[:8]}_{rank}_{language}_r{attempt}.mp4'
   while output.exists():attempt+=1;output=output.with_name(f'AutoCut_{id[:8]}_{rank}_{language}_r{attempt}.mp4')
   plan=make_plan(adapted,audio,production_copy(response),decision,title['text'],work,output,product,j.get('retain_original',False) or overlay_ok,fit_task,effective_sources(j,rank),music_only=music,target=language,source_frame_exceptions=j.get('source_frame_exceptions'))
   if x.get('supplement_plan'):plan['supplement_plan']=production_copy(x['supplement_plan']);write(work/'plans'/f'{rank}.json',plan)
   store.done(id,'RENDER_PLAN',rank,digest(plan),dict(path=str(work/'plans'/f'{rank}.json'),supplement_plan=x.get('supplement_plan')))
   store.done(id,'SUBTITLE',rank,digest(plan.get('final_script')),dict(status='NOT_APPLICABLE' if music else 'COMPLETE'))
   manager.check(id);store.item(id,rank,status='EDITING',plan=str(work/'plans'/f'{rank}.json'),render_revision=attempt,title=title['text'],final_title=x.get('final_title') or dict(text=title['text'],language=language,text_zh=title.get('text_zh','')),final_title_language=language,title_text_zh=title.get('text_zh',''),issues=[])
   manager.finish_plans(id,h,j,[plan],work)
   if sha(source['path'])!=source['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
   audit(store,id,rank,'ITEM_RESUMED',phase=phase,duration_match=match,supplement_plan=x.get('supplement_plan'),output=str(output),status=get_item(store,id,rank)[1]['status'])
 except Stopped:store.item(id,rank,status='INTERRUPTED',issues=['ITEM_INTERRUPTED'])
 except Exception as e:
  import traceback
  audit(store,id,rank,'ITEM_RECOVERY_ERROR',phase=phase,error=str(e),traceback=traceback.format_exc())
  code=getattr(e,'code',str(e));code=code if re.fullmatch('[A-Z][A-Z0-9_:,-]{1,100}',code) else 'JOB_REVIEW_REQUIRED'
  store.item(id,rank,status='WAITING_SCRIPT_APPROVAL' if code=='SCRIPT_CONFIRMATION_REQUIRED' else 'FAILED',issues=[code]);audit(store,id,rank,'ITEM_REVIEW_REQUIRED',reason=code)
 finally:
  latest=store.get(id);items=latest['items'];state='COMPLETED' if all(i['status']=='PASS' for i in items) else 'WAITING_SCRIPT_APPROVAL' if any(i.get('script_status')=='WAITING_APPROVAL' for i in items) and not latest.get('scripts_approved') else 'WAITING_TITLE_APPROVAL' if any(i['status']=='WAITING_TITLE_APPROVAL' for i in items) else 'PARTIAL_COMPLETED' if any(i['status']=='PASS' for i in items) else 'FAILED'
  # A completed/waiting peer must not turn live batch workers into user actions.
  with manager.lock:
   peers=any(job_id==id and other!=rank for job_id,other in manager.item_pending)
  if peers and latest.get('batch_review',{}).get('phase')=='RUNNING':store.update(id,state='RUNNING')
  else:store.update(id,state=state,stage='文案' if 'APPROVAL' in state else '检查')
