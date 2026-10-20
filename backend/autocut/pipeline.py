"""One shared pipeline for every AIDriver; copied engine owns rendering and QA."""
import copy,json,math,os,re,shutil,subprocess,threading,time
from pathlib import Path
from .file_storage import output_directory,preserve_job_sources
from concurrent.futures import ThreadPoolExecutor,as_completed
import httpx
from PIL import Image,ImageDraw
from .context import RES,HOME,ENGINE,asset
from .script_language import target_language,check,normalize_response,script_fields,subtitle_guard,VERSION
from .drivers import DriverError
from .remix import remix_items,apply_montage
from .copy_presentation import production_copy, copy_warnings, TranslationCache
from .provider_models import job_models,tts_payload
from .title_style import brief as title_brief
from .ownership import final_ready,read as state_read
from .workbench import selected_contexts,selected_script,approval_contexts,copy_candidates,assign_scripts
from .task_instructions import effective,effective_sources,copy_context,decision_override,bind_items,digest
from app.s3.headless_runtime import Harness,read,write,sha,fingerprint
from app.s3.headless_jobs import asr_job,tts_job
from app.s3.headless_media import preflight_job,approve_preflight,final_job,identity,batch_sheets
from app.s3.visual_preset import build_visual_plan,caption_events,fit_text,load_preset
from .subtitle_alignment import scaled_alignment
from app.s3.overlay_policy import lock_plan
from app.s3.deepseek_batch import generate_batch
from app.s2.deepseek import DeepSeekAdapter
from app.s2.providers import Official
from scripts.headless_transport import scan,qa_job

from .job_control import JobControl,Stopped
from .execution import RequestGate,AnalysisCache
from .parallel_tasks import MAX_PRODUCT_JOBS
from .source_analysis import analyze_sources

def copy_source_language(value):
 # Driver descriptions are evidence, not the bounded provider language field.
 from .script_language import canonical
 value=canonical(value)
 if re.search(r'\bmixed\b',value,re.I):return 'mixed'
 return value if 2<=len(value)<=40 else 'unknown'

def source_sheet(source,folder,overlay_policy=False):
 folder.mkdir(parents=True,exist_ok=True);dest=folder/'source.jpg'
 if dest.exists():return str(dest)
 times=[0,min(1.5,source['duration']*.1),source['duration']*.3,source['duration']*.55,source['duration']*.8,max(0,source['duration']-.5)]
 if overlay_policy:times=sorted({*times,*[float(t) for t in (1,2,3,4,5,7,10) if t<source['duration']]})
 points=[(source['path'],t) for t in times]
 if source.get('remix_clips'):points=[(c['path'],(c['source_start']+c['source_end'])/2) for c in source['remix_clips']]
 sheet=Image.new('RGB',(720,math.ceil(len(points)/3)*450),'#f3f3f4');draw=ImageDraw.Draw(sheet)
 for i,(video,t) in enumerate(points):
  p=folder/f'src{i}.png';subprocess.run(['ffmpeg','-nostdin','-v','error','-ss',str(t),'-i',video,'-frames:v','1','-vf','scale=240:426:force_original_aspect_ratio=decrease','-y',str(p)],capture_output=True,check=True,timeout=30)
  with Image.open(p) as im:sheet.paste(im,((i%3)*240,(i//3)*450))
  draw.text(((i%3)*240+6,(i//3)*450+428),f'{t:.1f}s',fill='black');p.unlink()
 sheet.save(dest,quality=92);return str(dest)

def make_plan(source,audio,copy_result,decision,approved_title,work,output,product,retain_original=False,task_overrides=None,override_sources=None,music_only=False,target=None,source_frame_exceptions=None):
 # Only this builder assembles the engine EditPlan. Driver output cannot provide commands/paths.
 language=target or target_language(dict(product_profile=product,language=copy_result.get('localized_script',{}).get('language')))
 spoken=None if music_only else check(copy_result.get('localized_script',{}),language)
 if (task_overrides or {}).get('title_enabled',True):check(next((t for t in copy_result.get('titles',[]) if t.get('text')==approved_title),dict(text=approved_title)),language,'TITLE')
 v=task_overrides or {};decision=decision_override(decision,v);d=source['duration'];a=audio['duration'];ranges=[[0,d]]
 if v.get('remove_redundant_sections') and not source.get('remix_clips') and decision.get('keep_ranges'):
  selected=decision['keep_ranges'];last=0
  if len(selected)>12:raise ValueError('CUSTOM_INSTRUCTION_RANGE_INVALID')
  for row in selected:
   if len(row)!=2 or not all(type(n) in (int,float) and math.isfinite(n) for n in row) or not last<=row[0]<row[1]<=d or row[1]-row[0]<.5:raise ValueError('CUSTOM_INSTRUCTION_RANGE_INVALID')
   last=row[1]
  ranges=selected;d=sum(b-a for a,b in ranges)
 from .duration_fit import solve
 fit=solve(d,a,dict(v,conservative_motion=True) if source.get('auto_supplement') else v,music_only)
 if not fit['fits']:raise ValueError('DURATION_FIT_FAILED')
 audio_speed=fit['voice_speed'];speed=fit['video_speed'];target=fit['target_duration']
 end=min(d,target*speed);remaining=end;kept=[]
 for lo,hi in ranges:
  take=min(hi-lo,remaining)
  if take>0:kept.append([lo,lo+take]);remaining-=take
 align=None if music_only else read(audio['alignment_path']);align=None if music_only else align.get('alignment') or align.get('normalized_alignment')
 if not music_only:subtitle_guard(spoken,language,align,audio)
 subtitle_style=copy.deepcopy(load_preset()['subtitle'])
 if 'subtitle_size_adjustment' in v:subtitle_style['font_size']={'smaller':48,'larger':56,'default':52}[v['subtitle_size_adjustment']]
 captions=[] if music_only else caption_events(scaled_alignment(align,audio_speed,target),style=subtitle_style)
 overrides=[]
 for layer,y,default in [('title',decision['title_y'],.17),('subtitle',decision['subtitle_y'],.78)]:
  if abs(y-default)>.001:overrides.append(dict(layer=layer,override_reason=decision['override_reason'] if decision['override_reason']!='NONE' else 'OTHER',chosen_position=[.5,y]))
 decision['cta_side']='left'
 visual=build_visual_plan(target,approved_title,captions,asset=asset(),overrides=overrides)
 title_enabled=v.get('title_enabled',True)
 if v:
  visual['task_rules']=dict(title_enabled=title_enabled,title_duration=v.get('title_duration',5.),subtitle_font_size=subtitle_style['font_size'])
  visual['title'].update(text=approved_title if title_enabled else '',end=min(v.get('title_duration',5.),target))
  visual['subtitle']['font_size']=subtitle_style['font_size']
  visual['user_revision']=dict(omit_added_title=not title_enabled)
 decision['cta_enabled']=v.get('cta_enabled',True)
 if not decision['cta_enabled']:
  visual['cta']['enabled']=False;visual.setdefault('user_revision',{}).update(omit_added_cta=True,reason='Task instruction or existing source arrow; source pixels preserved')
 if title_enabled:
  from .title_layout import apply as fit_title
  fit_title(visual,approved_title,max_lines=v.get('title_max_lines'))
 p=dict(rank=f'{source["rank"]:02}',source=dict(source,source_end=kept[-1][1]),source_ranges=kept,supplements=[],audio=dict(audio,audio_path=audio['path']),audio_speed=audio_speed,video_speed=speed,target_duration=target,title=approved_title,title_status='USER_APPROVED',approval_mode='USER_SELECTED_POLICY',visual=visual,output=str(output),usage_scope=product.get('usage_scope','production'),overlays=[],overlay_analysis={'status':'COMPLETE','reviewer':'AIDriver','retained_by_user':retain_original},subtitle_punctuation_removed=True)
 if v:
  p.update(task_overrides=copy.deepcopy(v),title_enabled=title_enabled,user_revision={'omit_added_title':not title_enabled},allow_propainter=False)
  p['parameter_sources']={key:dict(value=value,source=(override_sources or {}).get(key,'TASK_OVERRIDE')) for key,value in v.items()}
  p['resolved_parameters']={key:dict(value=value,source=(override_sources or {}).get(override,'TASK_OVERRIDE') if override in v else origin) for key,value,override,origin in [('cta_enabled',decision['cta_enabled'],'cta_enabled','AI_DECISION'),('title_enabled',title_enabled,'title_enabled','DEFAULT'),('title_duration',visual['title']['end'],'title_duration','PRESET'),('title_y',visual['title']['anchor_y'],'title_position_adjustment','AI_DECISION'),('subtitle_y',visual['subtitle']['anchor_y'],'subtitle_position_adjustment','AI_DECISION'),('subtitle_size',subtitle_style['font_size'],'subtitle_size_adjustment','PRESET'),('video_speed',speed,'video_speed_preference','AI_DECISION'),('audio_speed',audio_speed,'audio_speed_preference','DEFAULT')]}
 if source.get('auto_supplement'):
  from .auto_supplement import apply_montage as append_montage
  append_montage(p,source)
 elif source.get('remix_clips'):apply_montage(p,source)
 p['duration_fit']=fit
 if decision.get('subject_protection'):p['subject_protection']=copy.deepcopy(decision['subject_protection'])
 if decision.get('driver_provenance'):p['driver_provenance']=copy.deepcopy(decision['driver_provenance'])
 p['source_arrow_hint']=decision.get('source_arrow_hint',False)
 from .source_frame import bind as bind_source_frame
 if source_frame_exceptions:p['source_frame_exceptions']=copy.deepcopy(source_frame_exceptions)
 bind_source_frame(p)
 p.update(final_script=None if music_only else spoken['text'],final_script_language=None if music_only else language,target_language=language,final_title_language=language,final_language_version=VERSION)
 if music_only:p['voiceover_mode']='MUSIC_ONLY'
 if decision.get('title_blur_region'):
  from .title_blur_cover import bind
  try:bind(p,decision['title_blur_region'])
  except (ValueError,KeyError,TypeError):
   p['title_cover_note']='UNSAFE_OR_UNCERTAIN_REGION_KEEP_EXISTING_REPAIR'
  from .source_title_overlay import enabled
  if enabled(v) and not p.get('title_blur_white_cover'):
   p.setdefault('title_cover_warnings',[]).append('TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED')
 from .source_title_overlay import bind as bind_source_title
 bind_source_title(p,work)
 from .cta_policy import ensure_plan
 ensure_plan(p)
 lock_plan(p);write(work/'plans'/f'{source["rank"]:02}.json',p);return p


def preflight_review_context(plan,result,retained):
 # The preflight concatenates excerpts; sheet labels use its local time axis.
 # Send the mapping as evidence, never change captions or waive visual review.
 bank=read(result['qa_path']);segments=[];cursor=0.
 for lo,hi in result['selections']:
  segments.append(dict(preview_start=cursor,preview_end=cursor+hi-lo,plan_start=lo,plan_end=hi));cursor+=hi-lo
 frames=[]
 for sample in bank['samples']:
  t=sample['time'];seg=next((r for r in segments if r['preview_start']<=t<r['preview_end']),segments[-1]);pt=min(seg['plan_end'],seg['plan_start']+t-seg['preview_start'])
  captions=[c['text'] for c in plan['visual']['subtitle']['captions'] if c['start']<=pt<c['end']]
  frames.append(dict(sheet_time=t,plan_time=pt,expected_subtitles=captions))
 return dict(plan=plan,preview_sha256=result['output_sha256'],retained_source=retained,technical_status=result['technical_status'],preview_time_axis='Concatenated excerpts. Sheet timestamps are preview-local; compare against mapped plan_time, not sheet_time.',preview_segments=segments,frame_caption_evidence=frames)

class Manager:
 def __init__(self,store,secrets,router):
  self.store,self.secrets,self.router=store,secrets,router;self.queue=ThreadPoolExecutor(max_workers=MAX_PRODUCT_JOBS,thread_name_prefix='job');self.active={};self.item_pending=set();self.lock=threading.RLock();self.stops={};self.control=JobControl(self);self.visual_gate=RequestGate(2);self.analysis_cache=AnalysisCache(store.home)
 def enqueue(self,id):
  with self.store.lock,self.lock:
   if id in self.active and not self.active[id].done():return
   if getattr(self,'confirmation_wait',None) and self.confirmation_wait.defer(id,dict(kind='job')):return
   spec=dict(kind='job');self.control.register(id,spec)
   self.stops[id]=threading.Event();self.active[id]=self.queue.submit(self.control.run,id,self.run,id)
   self.control.track(id,self.active[id],spec)
 def enqueue_item(self,id,rank,phase='SCRIPT'):
  key=(id,f'{int(rank):02}')
  with self.store.lock,self.lock:
   if key in self.item_pending:return
   if getattr(self,'confirmation_wait',None) and self.confirmation_wait.defer(id,dict(kind='item',rank=key[1],phase=phase)):return
   spec=dict(kind='items',rows=[[key[1],phase]]);self.control.register(id,spec)
   from .ownership import invalidate_final
   invalidate_final(self.store,id,rank)
   self.item_pending.add(key);self.store.item(id,rank,status='QUEUED',resume_phase=phase)
  def launch(_=None):
   with self.store.lock,self.lock:
    old=self.active.get(id)
    if old and not old.done():old.add_done_callback(launch);return
    self.stops[id]=threading.Event()
    from .item_pipeline import resume_item
    def work():
     try:self.control.run(id,resume_item,self,id,rank,phase)
     finally:
      with self.lock:self.item_pending.discard(key)
    self.active[id]=self.queue.submit(work)
    self.control.track(id,self.active[id],spec)
  launch()
 def enqueue_batch(self,id,rows):
  # One release barrier; two independent item workers. Waiting/failed items return
  # without preventing peers from completing, and shared stage caches still apply.
  with self.store.lock,self.lock:
   rows=[(f'{int(rank):02}',phase) for rank,phase in rows if (id,f'{int(rank):02}') not in self.item_pending]
   if not rows:return
   if getattr(self,'confirmation_wait',None) and self.confirmation_wait.defer(id,dict(kind='items',rows=[[rank,phase] for rank,phase in rows])):return
   spec=dict(kind='items',rows=[[rank,phase] for rank,phase in rows]);self.control.register(id,spec)
   self.item_pending.update((id,rank) for rank,_ in rows)
  def launch(_=None):
   with self.store.lock,self.lock:
    old=self.active.get(id)
    if old and not old.done():old.add_done_callback(launch);return
    self.stops[id]=threading.Event()
    def work():
     from .item_pipeline import resume_item
     def one(row):
      rank,phase=row
      try:
       with self.control.worker(id):
        self.store.item(id,rank,resume_pending=False,resume_running=True)
        resume_item(self,id,rank,phase)
      except Stopped:pass
      except Exception:
       self.store.item(id,rank,status='FAILED',issues=['INTERNAL_PRODUCTION_ERROR'])
      finally:
       self.store.item(id,rank,resume_running=False)
       with self.lock:self.item_pending.discard((id,rank))
     try:
      with ThreadPoolExecutor(max_workers=2,thread_name_prefix='batch-item') as pool:list(pool.map(one,rows))
     finally:
      items=self.store.get(id)['items'];done=all(x['status']=='PASS' for x in items)
      self.store.update(id,state='COMPLETED' if done else 'PARTIAL_COMPLETED' if any(x['status']=='PASS' for x in items) else 'FAILED',stage='检查')
    self.active[id]=self.queue.submit(work)
    self.control.track(id,self.active[id],spec)
  launch()
 def resume_job_after_user_action(self,id,recover=False):
  from .production_resume import resume
  return resume(self,id,recover)
 def recover_jobs(self):
  for j in self.store.jobs():
   if j.get('deleted_at') or j.get('control_request'):continue
   if j['state']=='INTERRUPTED' and any(s.get('kind')=='job' for s in j.get('control_resume',[])):
    self.enqueue(j['id'])
   elif j['state'] not in {'COMPLETED','STOPPING'}:self.resume_job_after_user_action(j['id'],True)
 def prepare_batch_titles(self,id,ranks):
  with self.store.lock,self.lock:
   if any((id,r) in self.item_pending for r in ranks):return
   if getattr(self,'confirmation_wait',None) and self.confirmation_wait.defer(id,dict(kind='titles',ranks=list(ranks))):return
   spec=dict(kind='titles',ranks=list(ranks));self.control.register(id,spec)
   self.item_pending.update((id,r) for r in ranks)
   self.store.update(id,state='RUNNING',stage='标题')
  def launch(_=None):
   with self.store.lock,self.lock:
    old=self.active.get(id)
    if old and not old.done():old.add_done_callback(launch);return
    def work():
     from .content_confirmation import ContentConfirmation
     from .item_review import ItemReview,get_item
     from .copy_presentation import TranslationService
     from .batch_confirmation import valid_title
     translations=TranslationService(self.store.home,self.secrets);review=ItemReview(self.store,self,self.router,translations);service=ContentConfirmation(self.store,self,review,translations)
     failed=False
     for rank in ranks:
      try:
       self.check(id);j,x=get_item(self.store,id,rank)
       titles=[translations.cache.row(t,job_models(j)['deepseek']) for t in x.get('title_candidates') or []]
       if len(titles)==3 and all(valid_title(t,j) for t in titles):
        if not all(valid_title(t,j) for t in x.get('title_candidates') or []):
         self.store.item(id,rank,title_candidates=titles,copy=dict(x['copy'],titles=titles),status='WAITING_TITLE_APPROVAL',operation_inflight=False,issues=[])
        continue
       self.store.item(id,rank,status='SCRIPTING',operation_inflight=True)
       # Existing valid target-language titles only need their missing comparison
       # translations. Do not regenerate copy that the user has already seen.
       from .script_language import check as check_language
       reusable=len(titles)==3 and len({str(t.get('id')) for t in titles})==3
       if reusable:
        try:
         for t in titles:check_language(t,target_language(j),'TITLE',require_metadata=True)
        except ValueError:reusable=False
       if reusable:
        titles=translations.translate(titles,job_models(j)['deepseek'])['rows']
        if not all(valid_title(t,j) for t in titles):raise ValueError('CONTENT_TRANSLATION_REQUIRED')
       else:titles=service.title_request(j,x)
       for t in titles:translations.cache.put(t['text'],target_language(j),job_models(j)['deepseek'],t['text_zh'])
       response=dict(x['copy'],titles=titles)
       self.store.item(id,rank,status='WAITING_TITLE_APPROVAL',copy=response,title_candidates=titles,batch_selected_title_id=titles[0]['id'],operation_inflight=False,issues=[])
      except Stopped:raise
      except Exception:
       failed=True;self.store.item(id,rank,status='FAILED',operation_inflight=False,issues=['TITLE_GENERATION_FAILED'])
      finally:
       with self.lock:self.item_pending.discard((id,rank))
     self.store.update(id,state='FAILED' if failed else 'WAITING_TITLE_APPROVAL',title_prepare_failed=failed)
    def controlled_titles():
     try:self.control.run(id,work)
     finally:
      with self.lock:self.item_pending.difference_update((id,r) for r in ranks)
    self.active[id]=self.queue.submit(controlled_titles)
    self.control.track(id,self.active[id],spec)
  launch()
 def stop(self,id):return self.control.request(id,'STOP')
 def check(self,id):self.control.check(id)
 def judge(self,id,h,stage,item,inputs,method,images):
  self.check(id)
  # Old compact analyses omitted subject evidence; do not reuse that contract.
  key=fingerprint(dict(contract='SUBJECT_EVIDENCE_V1',inputs=inputs)) if stage in {'VIDEO_ANALYSIS','EDIT_PLAN'} else fingerprint(inputs)
  if stage=='VISUAL_QA':key=fingerprint(dict(contract='VISUAL_SEVERITY_V1',inputs=inputs))
  old=self.store.checkpoint(id,stage,item,key)
  cache=getattr(self,'analysis_cache',None)
  shared=stage=='VIDEO_ANALYSIS' and isinstance(cache,AnalysisCache)
  if old and shared and (not cache.valid(old,inputs) or (old.get('driver_provenance') or {}).get('image_sha256')!=[sha(p) for p in images]):old=None
  if old:
   with h.profiler.span(stage,item,cache_hit=True):pass
   if shared:
    # Backfill the shared cache from a previously validated job checkpoint.
    drivers=getattr(self.router,'drivers',{})
    driver=drivers.get((old.get('driver_provenance') or {}).get('actual_driver')) if isinstance(drivers,dict) else None
    if driver:
     cached_key=cache.key(inputs,images,driver)
     with cache.lock(cached_key,lambda:self.check(id)):
      if not cache.load(cached_key,inputs):cache.save(cached_key,inputs,old,dict(job_id=id,rank=str(item)))
   return old
  legacy=self.store.checkpoint(id,stage,item,fingerprint(inputs)) if stage=='VIDEO_ANALYSIS' else None
  requested=self.store.settings().get('ai_driver','auto');driver=self.router.resolve(requested)
  cached_key=cache.key(inputs,images,driver) if shared else None
  if shared:
   with cache.lock(cached_key,lambda:self.check(id)):
    cached=cache.load(cached_key,inputs)
    if cached:
     result=copy.deepcopy(cached['result']);result.setdefault('driver_provenance',{}).update(cache_hit=True,cache_origin=cached.get('origin'),stage=stage,rank=str(item))
     if result.get('title_blur_region'):result['title_blur_region'].update(source_sha256=inputs['source']['sha256'],evidence=[dict(path=str(p),sha256=sha(p)) for p in images])
     with h.profiler.span(stage,item,cache_hit=True):pass
     self.store.done(id,stage,item,key,result);return result
    result=self._judge_request(id,h,stage,item,inputs,method,images,driver,requested,legacy)
    cache.save(cached_key,inputs,result,dict(job_id=id,rank=str(item)));return result
  return self._judge_request(id,h,stage,item,inputs,method,images,driver,requested,legacy)
 def _judge_request(self,id,h,stage,item,inputs,method,images,driver,requested,legacy=None):
  key=fingerprint(dict(contract='SUBJECT_EVIDENCE_V1',inputs=inputs)) if stage in {'VIDEO_ANALYSIS','EDIT_PLAN'} else fingerprint(inputs)
  if stage=='VISUAL_QA':key=fingerprint(dict(contract='VISUAL_SEVERITY_V1',inputs=inputs))
  gate=getattr(self,'visual_gate',None)
  for attempt in range(2):
   try:
    with h.profiler.span('AUTO_REWORK' if stage=='REPAIR_PLAN' else stage,item) as event:
     if isinstance(gate,RequestGate):
      with gate.enter(lambda:self.check(id)) as delay:
       event['queue_wait_seconds']=delay;result=getattr(driver,method)(inputs,images).model_dump()
     else:result=getattr(driver,method)(inputs,images).model_dump()
    break
   except DriverError as exc:
    if isinstance(gate,RequestGate) and (getattr(exc,'category',None)=='RATE_LIMIT' or exc.code=='AI_DRIVER_RATE_LIMITED'):gate.rate_limited()
    # Only transient visual-judgment errors may retry once. Never replay ASR,
    # localization, TTS or rendering, and never bypass a failed visual verdict.
    if stage!='VISUAL_QA' or attempt or not exc.retryable:raise
    from .item_review import audit
    audit(self.store,id,item,'AI_STAGE_RETRY',stage=stage,attempt=1,error=exc.code,category=getattr(exc,'category','TIMEOUT'))
    for _ in range(5):
     self.check(id);time.sleep(1)
    self.check(id)
  if legacy:
   # Enrich existing visual evidence without invalidating paid copy or ASR.
   # A changed source/context has a different fingerprint and cannot enter here.
   result={**legacy,**{k:result[k] for k in ['people_present','product_description','main_actions','protected_regions']}}
  evidence=driver.request_evidence() if hasattr(driver,'request_evidence') else {}
  fallback=requested=='auto' and driver.name!='codex'
  evidence.update(requested_driver=requested,actual_driver=driver.name,fallback_used=fallback,fallback_reason='CODEX_NOT_READY' if fallback else None,stage=stage,rank=str(item),at=time.time())
  result['driver_provenance']=evidence
  if stage=='VIDEO_ANALYSIS':result['visual_evidence_version']='SUBJECT_EVIDENCE_V1'
  if stage=='VIDEO_ANALYSIS' and result.get('title_blur_region'):
   result['title_blur_region'].update(source_sha256=inputs['source']['sha256'],evidence=[dict(path=str(p),sha256=sha(p)) for p in images])
  if stage=='EDIT_PLAN' and method=='build_edit_plan':
   a=inputs.get('analysis',{})
   result['source_arrow_hint']=bool(a.get('existing_arrow'))
   result['subject_protection']={k:copy.deepcopy(a.get(k)) for k in ['visual_evidence_version','people_present','product_description','main_actions','protected_regions']}
   result['subject_protection']['layout_reason']=result.get('subject_avoidance_reason','')
   if a.get('title_blur_region'):result['title_blur_region']=copy.deepcopy(a['title_blur_region'])
  self.store.done(id,stage,item,key,result)
  with self.store.lock:
   j=self.store.get(id);history=j['driver_history']
   history.append(dict(driver=driver.name,**evidence))
   self.store.update(id,ai_driver=driver.name,driver_history=history)
  return result
 def catalog(self,model=None):
  # Existing ElevenLabs catalog contract, separate AutoCut cache and Keychain.
  p=HOME/'cache/tts/catalog.json';key=self.secrets.get('ELEVENLABS_API_KEY')
  if not key:raise ValueError('ELEVENLABS_NOT_CONFIGURED')
  keytag=fingerprint(key);old=read(p)
  if old and old.get('credential_fingerprint')==keytag and (not model or any(m['id']==model for m in old.get('models',[]))):return old
  provider=Official('elevenlabs',key)
  try:cat=provider.catalog()
  finally:provider.client.close()
  cat.update(scope=fingerprint(['elevenlabs',cat.get('account_id',keytag)]),credential_fingerprint=keytag);write(p,cat);return cat
 def copy_batch(self,contexts,work,model='deepseek-chat'):
  shared=HOME/'cache/deepseek';shared.mkdir(parents=True,exist_ok=True)
  link=Path(work)/'scripts/deepseek';link.parent.mkdir(parents=True,exist_ok=True)
  if not link.exists():link.symlink_to(shared,target_is_directory=True)
  # Credentials remain in the adapter object, never global environment or artifacts.
  def factory():
   key=self.secrets.get('DEEPSEEK_API_KEY')
   if not key:raise ValueError('DEEPSEEK_NOT_CONFIGURED')
   adapter=object.__new__(DeepSeekAdapter);adapter._key=key;adapter.client=httpx.Client(timeout=httpx.Timeout(90,connect=10),follow_redirects=False,trust_env=False)
   if contexts and all('PRESERVE_SCRIPT_FIRST_V1:' in c.get('video_context','') for c in contexts):
    from .production_preflight import COPY_POLICY
    original=adapter.complete
    def complete(model,messages,**kwargs):
     messages=copy.deepcopy(messages);messages[0]['content']+='\n'+COPY_POLICY
     return original(model,messages,**kwargs)
    adapter.complete=complete
   return adapter
  return generate_batch(contexts,work,adapter_factory=factory,model=model)
 def prepare_copies(self,contexts,work,model):
  path=Path(work)/'scripts/copy_results.json';copies=read(path,{})
  # Only pre-request validation failures are safe to retry here. Never replay
  # a paid/uncertain attempt or regenerate the other successful manuscripts.
  pending=[c for c in contexts if c['script_group_id'] not in copies or copies[c['script_group_id']].get('error')=='INVALID_DEEPSEEK_CONTEXT']
  if pending:
   result=self.copy_batch(pending,work,model)
   copies.update({r['script_group_id']:r for r in result['items']});write(path,copies)
  return copies
 def prepare_remix(self,id,h,job,product,sources,analyses,sheets,language,work):
  usable=[s for s in sources if s['rank'] in analyses]
  if len(usable)<2:raise ValueError('REMIX_NEEDS_SOURCES')
  evidence=[dict(rank=s['rank'],sha256=s['sha256'],duration=s['duration'],analysis=analyses[s['rank']],**({'usable_intervals':s['source_health']['usable_intervals']} if s.get('source_health') else {})) for s in usable]
  count=job.get('output_count') or 10
  composition=dict(output_count=count,product=product,language=language,sources=evidence,image_source_ranks=[s['rank'] for s in usable],script_reference=job.get('reference',{}))
  if job.get('resolved_overrides_json'):composition['task_overrides']=job['resolved_overrides_json']
  custom=bool(job.get('custom_remix'))
  if custom:
   from .custom_remix import selection,source_priorities
   folder_order=job['custom_remix'].get('source_folder_priority',[])
   priorities=source_priorities(sources,job['input_path'],folder_order) if folder_order else None
   selected=selection(sources,analyses,count,fast_interleaved=job['custom_remix'].get('cut_style')=='FAST_INTERLEAVED',priorities=priorities)
  else:selected=self.judge(id,h,'EDIT_PLAN','remix-composition',composition,'compose_remix',[sheets[s['rank']] for s in usable])
  items,mapped=remix_items(selected,usable,analyses,count,selling_points_primary=custom)
  if custom and job['custom_remix'].get('source_folder_priority'):
   if {c['sha256'] for item in items for c in item['remix_clips']}!={s['sha256'] for s in sources}:raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
  for item in items:
   if effective(job,item['rank']).get('preserve_source_structure'):
    ends={}
    for c in item['remix_clips']:
     if c['source_start']<ends.get(c['source_rank'],0):raise ValueError('REMIX_PLAN_INVALID')
     ends[c['source_rank']]=c['source_end']
  contexts=[];mapping={};group_context={};images={}
  for item in items:
   rank=item['rank'];a=mapped[rank]
   images[rank]=source_sheet(item,work/'remix'/f'{rank:02}')
   # Distinct visual angles remain distinct contexts; normal DeepSeek cache still applies.
   group_values=[a['clean_script'],a['allowed_claims'],a['promotion_context'],a['context'],language]
   if effective(job,rank):group_values.append(effective(job,rank))
   group=fingerprint(group_values)
   mapping[rank]=group
   if group not in group_context:
    c=dict(script_group_id=group,dedupe_reviewed=True,source_language=a['source_language'],source_script=a['clean_script'],clean_script=a['clean_script'],video_context=a['context'],product=product['product_name'],supported_selling_points=a['selling_points'],allowed_claims=a['allowed_claims'],blocked_claims=[],promotion_context=a['promotion_context'] or 'Natural shopping CTA authorized. Do not invent prices, stock or discounts.',target_duration_seconds=item['duration'],market=job['market'],language=language,title_count=3,title_brief=title_brief(json.dumps(job.get('reference',{}),ensure_ascii=False),remix=True))
    c=copy_context(c,job,rank);c['script_group_id']=group;group_context[group]=c;contexts.append(c)
   self.store.item(id,rank,source_snapshot=item,filename=item['filename'],status='ANALYZING',duration=item['duration'],analysis=a,voice_quality=a['voice_quality'],source_ranks=[c['source_rank'] for c in item['remix_clips']],creative_angle=item['creative_angle'],issues=[])
  if custom:
   from .custom_remix import contexts as custom_contexts
   contexts,mapping,group_context=custom_contexts(job,items,mapped,product,language,title_brief('',remix=True))
   used=sorted({c['source_rank'] for item in items for c in item['remix_clips']})
   coverage=dict(source_ranks=used,source_count=len(sources),output_durations=[s['duration'] for s in items],script_groups=job['custom_remix']['script_groups'],music_only_ranks=job['custom_remix']['music_only_ranks'])
   if folder_order:coverage.update(source_folder_priority=folder_order,opening_source_ranks=[item['remix_clips'][0]['source_rank'] for item in items],source_sha256=sorted({s['sha256'] for s in sources}))
   self.store.done(id,'CUSTOM_REMIX_COVERAGE','all',fingerprint(selected),coverage)
  write(work/'plans/remix_selection.json',selected)
  return items,mapped,images,contexts,mapping,group_context
 def run(self,id):
  started=time.monotonic();job=self.store.get(id);models=job_models(job);work=HOME/'jobs'/id/'work';work.mkdir(parents=True,exist_ok=True)
  try:
   self.check(id);self.store.update(id,state='RUNNING',stage='分析');product=job.get('product_profile') or dict(self.store.product(job['product_profile_id']),product_name=job['product_name']);remix=job.get('mode')=='Remix';language=target_language(job)
   self.store.update(id,target_language=language)
   job=preserve_job_sources(self.store,job,lambda:self.check(id))
   with Harness(work,render_workers=2) as h:
    manifest=read(work/'manifest.json')
    folders=(job.get('custom_remix') or {}).get('source_folder_priority',[])
    if not manifest and folders:
     from .custom_remix import priority_paths
     sources=scan(h,Path(job['input_path']),paths=priority_paths(job['input_path'],folders))
    else:sources=manifest['sources'] if manifest else scan(h,Path(job['input_path']))
    if job.get('source_names'):
     if (job.get('submission_origin') or {}).get('type')=='feishu':
      from .feishu_tasks.source_order import ordered_sources
      sources=ordered_sources(sources,job['source_names']);write(work/'manifest.json',dict(sources=sources))
     else:sources=[s for s in sources if s.get('filename') in job['source_names']]
    self.store.update(id,source_count=len(sources))
    if not sources:raise ValueError('NO_VALID_SOURCE')
    if job.get('custom_instruction_raw') or job.get('task_tag_snapshot'):
     binding,missing=bind_items(job,sources)
     fields=dict(instruction_item_bindings=binding)
     if missing:
      fields.update(custom_instruction_status='PARTIAL',instruction_issues=list(dict.fromkeys(job.get('instruction_issues',[])+['CUSTOM_INSTRUCTION_PARTIAL'])),instruction_summary=job.get('instruction_summary',[])+['超出素材编号的要求未应用'])
     self.store.update(id,**fields)
    valid=[s for s in sources if s['status']=='VALID'];original_sources=valid;analyses={};sheets={};contexts=[];mapping={};group_context={};transcripts={}
    if not remix:
     assignment=assign_scripts(job,valid)
     if assignment:self.store.update(id,**assignment);job.update(assignment)
    asr=[]
    for s in sources:
     self.check(id)
     if not remix:self.store.item(id,s['rank'],filename=s.get('filename',Path(s['path']).name),status='ANALYZING' if s['status']=='VALID' else 'REVIEW_REQUIRED',duration=s.get('duration'),source_duration=s.get('duration'),source_path=s['path'],source_snapshot=s,issues=[] if s['status']=='VALID' else ['INVALID_SOURCE'])
     if s['status']!='VALID':continue
     # Shared exact-source/model ASR cache; original files stay in place.
     cached=read(HOME/'cache/asr'/f'{s["sha256"]}.index.json',{})
     asr.append((s,self.control.stage(id,asr_job(h,s,ENGINE/'models/ggml-base.bin',binary=RES/'runtime/media/bin/whisper-cli',existing_transcript=cached.get('path')))))
    prepared=analyze_sources(self,id,h,job,product,asr,work,remix)
    for entry in prepared:
     s=entry['source'];self.check(id);r=entry['asr_result']
     if r.get('error') and (remix or job.get('script_mode','AUTO')!='AUTO'):
      if not remix:self.store.item(id,s['rank'],status='REVIEW_REQUIRED',issues=[r['error']])
      continue
     if r.get('error'):r=dict(text='',asr_error=r['error'])
     path=entry['transcript_path'];transcript=entry['transcript'];transcripts[s['rank']]=transcript
     sheet=entry['sheet'];sheets[s['rank']]=sheet;task=entry['task'];a=entry['analysis'];analyses[s['rank']]=a
     if not remix:self.store.item(id,s['rank'],source_script=transcript.get('text',''),source_language=transcript.get('language','unknown'),source_sheet=sheet)
     if not remix:self.store.item(id,s['rank'],voice_quality=a['voice_quality'],analysis=a,clean_source_script=a.get('clean_script',''))
     if remix:
      self.store.done(id,'ASR_DONE',s['rank'],s['sha256'],dict(transcript=path));continue
     fallback=a['voice_quality'] in ['POOR','UNUSABLE','NO_SPEECH'] and not job.get('script_snapshot')
     if fallback:
      if not a.get('selling_points') or not a.get('context'):self.store.item(id,s['rank'],status='REVIEW_REQUIRED',issues=['NO_RELIABLE_SCRIPT']);continue
      a['clean_script']='';self.store.item(id,s['rank'],script_source={'type':'SELLING_POINTS'},script_fallback=True)
     fallback_basis='No reliable source speech. Generate a new target-market sales script ONLY from this video/product evidence: '+a.get('context','')+'; selling points: '+', '.join(a.get('selling_points',[]))
     # Explicit semantic grouping preserves prices/effects in the fact fingerprint.
     group=fingerprint([a['group_key'],a['allowed_claims'],a['promotion_context'],language,{k:v for k,v in task.items() if k in {'copy_style','localization_style','title_direction','script_reference_mode','reference_selector','promotion_intensity','preserve_source_structure'}}]) if task else fingerprint([a['group_key'],a['allowed_claims'],a['promotion_context'],language])
     mapping[s['rank']]=group
     if group not in group_context:
      c=dict(script_group_id=group,dedupe_reviewed=True,source_language=copy_source_language(a['source_language']),source_script=fallback_basis if fallback else transcript.get('text') or a['clean_script'],clean_script=fallback_basis if fallback else a['clean_script'],video_context=a['context'],product=product['product_name'],supported_selling_points=a['selling_points'],allowed_claims=a['allowed_claims'],blocked_claims=[],promotion_context=a['promotion_context'] or 'A natural shopping CTA is authorized. Do not invent prices, stock or discounts.',target_duration_seconds=s['duration'],market=job['market'],language=language,title_count=3,title_brief=title_brief(json.dumps(job.get('reference',{}),ensure_ascii=False)));c=copy_context(c,job,rank=s['rank']);c['script_group_id']=group;group_context[group]=c;contexts.append(c)
     self.store.done(id,'ASR_DONE',s['rank'],s['sha256'],dict(transcript=path))
    if remix:
     valid,analyses,sheets,contexts,mapping,group_context=self.prepare_remix(id,h,job,product,valid,analyses,sheets,language,work)
     assignment=assign_scripts(job,valid)
     if assignment:self.store.update(id,**assignment);job.update(assignment)
    # The chosen script is immutable job evidence, never a mutable table row.
    contexts,mapping,group_context=selected_contexts(contexts,mapping,job)
    self.check(id);self.store.update(id,stage='文案',progress=20)
    for s in valid:
     if s['rank'] in mapping:self.store.item(id,s['rank'],status='LOCALIZING')
    if not contexts:
     self.store.update(id,state='REVIEW_REQUIRED',issues=['NO_RELIABLE_SCRIPT'],elapsed=time.monotonic()-started);return
    with h.profiler.span('DEEPSEEK','batch'):copies=self.prepare_copies(approval_contexts(contexts,job),work,models['deepseek'])
    decisions={};copy_by_rank={}
    for s in valid:
     rank=s['rank'];group=mapping.get(rank);r=copies.get(group,{})
     if rank not in mapping:continue
     if r.get('error') or not r.get('response'):self.store.item(id,rank,status='REVIEW_REQUIRED',issues=['LOCALIZATION_REQUIRED' if r.get('error')=='INVALID_DEEPSEEK_OUTPUT' else r.get('error','COPY_UNAVAILABLE')]);continue
     try:
      r=dict(r,response=normalize_response(r['response'],job))
     except ValueError:
      from .item_review import ItemReview,get_item
      from .copy_presentation import TranslationService
      try:r=dict(r,response=ItemReview(self.store,self,self.router,TranslationService(self.store.home,self.secrets)).localize_response(job,get_item(self.store,id,rank)[1],r['response']))
      except Exception:
       self.store.item(id,rank,status='REVIEW_REQUIRED',issues=['FINAL_SCRIPT_LANGUAGE_MISMATCH']);continue
     copies[group]=r
     candidates=copy_candidates(copies,group)
     valid_candidates=[]
     for candidate in candidates:
      try:
       checked=normalize_response(candidate['response'],job);candidate=dict(candidate,language=language,response=checked);check(candidate,language);valid_candidates.append(candidate)
      except ValueError:pass
     candidates=valid_candidates
     if job.get('script_mode')=='REQUIRE_APPROVAL':
      self.store.item(id,rank,script_candidates=candidates,script_status='USER_APPROVED' if job.get('scripts_approved') else 'WAITING_APPROVAL')
      self.store.done(id,'SCRIPT_GENERATED',rank,fingerprint(candidates),dict(candidates=candidates))
      selected=job.get('script_selections',{}).get(f'{rank:02}')
      if job.get('scripts_approved'):
       if not selected:continue
       if selected['candidate_sha']!=digest(candidates):raise ValueError('SCRIPT_SELECTION_INVALID')
       r=dict(r,response=selected['response'])
     r=dict(r,response=normalize_response(r['response'],job));copy_by_rank[rank]=r['response'];a=analyses[rank]
     music=bool(job.get('custom_remix') and rank in job['custom_remix']['music_only_ranks'])
     self.store.item(id,rank,final_language_version=VERSION,**script_fields(r['response'],job,music),**({'music':job['custom_remix_music'][f'{rank:02}']} if music else {}))
     self.store.done(id,'FINAL_SCRIPT_LANGUAGE_CHECK',rank,fingerprint(r['response']['localized_script']),dict(status='EXEMPT_MUSIC_ONLY' if music else 'PASS',target_language=language))
     comparison_cache=TranslationCache(self.store.home)
     for text_row in [r['response']['localized_script'],*r['response']['titles']]:comparison_cache.put(text_row['text'],language,models['deepseek'],text_row.get('text_zh'))
     self.store.item(id,rank,presentation_warnings=copy_warnings(r['response']))
     decision_context=dict(analysis=a,copy=production_copy(r['response']),duration=s['duration'],language=language,visual_preset=load_preset())
     task=effective(job,rank)
     if task:decision_context.update(task_overrides=task,mode=job.get('mode','Transport'),asr_timing=transcripts.get(rank,{}).get('segments',[]))
     if remix:decision_context.update(mode='Remix',remix_clips=s['remix_clips'])
     # Both Web and Feishu build the plan after the final content barrier.
     # The item worker uses the approved text, so a prior decision is redundant.
     defer_plan=bool(job.get('batch_review_required') or job.get('content_confirmation_required')) and not job.get('titles_approved')
     dec=None if defer_plan else self.judge(id,h,'EDIT_PLAN',rank,decision_context,'build_edit_plan',[sheets[rank]])
     # A ready localized manuscript is the default at the batch human barrier.
     # Editorial wording suggestions remain metadata, not another approval gate.
     if dec and not dec['copy_approved'] and not job.get('batch_review_required'):self.store.item(id,rank,status='REVIEW_REQUIRED',issues=dec['issues'] or ['COPY_REVIEW_REQUIRED']);continue
     dec=decision_override(dec,task) if dec else None;decisions[rank]=dec
     self.store.item(id,rank,status='COPY_READY',title_candidates=r['response']['titles'],selected_title_id=dec['selected_title_id'] if dec else r['response']['titles'][0]['id'],copy=r['response'],decision=dec,task_overrides=task,script_source=selected_script(job,rank) or ({'type':'SELLING_POINTS'} if a['voice_quality'] in ['POOR','UNUSABLE','NO_SPEECH'] else {'type':'AUTO'}),script_status='USER_APPROVED' if job.get('scripts_approved') else 'WAITING_APPROVAL' if job.get('script_mode')=='REQUIRE_APPROVAL' else 'AUTO',title_enabled=task.get('title_enabled',True))
     self.store.done(id,'COPY_DONE',rank,fingerprint(group_context[group]),r['response'])
    if not decisions:
     self.store.update(id,state='REVIEW_REQUIRED',stage='文案',issues=['NO_RELIABLE_SCRIPT'],elapsed=time.monotonic()-started);return
    if job.get('content_confirmation_required') or (job.get('script_mode')=='REQUIRE_APPROVAL' and not job.get('scripts_approved')) or (job['title_approval_mode']=='manual' and not job.get('titles_approved')):
     for rank in decisions:self.store.item(id,rank,status='WAITING_SCRIPT_APPROVAL',script_status='WAITING_APPROVAL',content_edit=True)
     self.store.update(id,state='WAITING_SCRIPT_APPROVAL',stage='文案',progress=30);return
    approvals=job.get('title_selections',{})
    self.check(id);self.store.update(id,stage='配音',progress=35)
    cat=self.catalog(models['elevenlabs']);tts=[];plans=[];prior=[]
    from .production_preflight import receipts
    prior=receipts(HOME)
    for s in valid:
     self.check(id);rank=s['rank']
     if rank not in decisions:continue
     a=analyses[rank];r=copy_by_rank[rank];decision=decisions[rank]
     task=effective(job,rank)
     retain_title=task.get('retain_source_title') and a.get('only_source_title_overlay')
     if a['overlays_need_review'] and not (job.get('retain_original') or retain_title):
      self.store.item(id,rank,qa_warnings=['OVERLAY_REQUIRES_PREVIEW'])
     tid=int(approvals.get(f'{rank:02}',decision['selected_title_id']));title=next(t['text'] for t in r['titles'] if t['id']==tid)
     voices=cat['voices'];preferred=product.get('voice_preferences',{}).get(a['speaker'])
     v=next((v for v in voices if v['id']==preferred),None)
     if not v:
      names=['Lucas','Victor Pinho'] if a['speaker']=='male' else ['Carla']
      v=next((v for name in names for v in voices if name.casefold() in v['name'].casefold()),None)
     if not v:self.store.item(id,rank,status='REVIEW_REQUIRED',issues=['MATCHED_VOICE_UNAVAILABLE']);continue
     check(r['localized_script'],language);check(next(t for t in r['titles'] if t['id']==tid),language,'TITLE')
     payload,_=tts_payload(r['localized_script']['text'],v['id'],models['elevenlabs'],cat,language)
     from .production_preflight import require_title,before_tts
     from .review_media import base_clips
     current=self.store.get(id);item=next(x for x in current['items'] if int(x['rank'])==rank)
     try:require_title(current,item,title)
     except ValueError as error:self.store.item(id,rank,status='REVIEW_REQUIRED',issues=[str(error)]);continue
     if not before_tts(self.store,current,item,payload,cat['scope'],prior,base_clips(s,decision,task)):continue
     review=dict(title_status='USER_APPROVED',script_status='CODEX_APPROVED',reviewer=self.store.get(id).get('ai_driver'),approval_mode='MANUAL' if job['title_approval_mode']=='manual' else 'USER_DELEGATED_AUTOMATIC',tts_text_sha256=fingerprint(payload['tts_text']))
     self.store.done(id,'TITLE_APPROVED',rank,fingerprint(r),dict(title=title,review=review));self.store.item(id,rank,status='VOICE',title=title,final_title=dict(text=title,language=language),final_title_language=language,title_text_zh=next(t.get('text_zh','') for t in r['titles'] if t['id']==tid),voice=v['name'],title_status='USER_APPROVED' if job['title_approval_mode']=='manual' else 'AUTO')
     future=tts_job(h,payload,scope=cat['scope'],provider_factory=lambda:Official('elevenlabs',self.secrets.get('ELEVENLABS_API_KEY')),review=review,existing_results=prior,execute=True)
     tts.append((s,self.control.stage(id,future),title))
    for s,f,title in tts:
     self.check(id);r=f.result()
     if r.get('error') or r.get('technical_status')!='PASS':self.store.item(id,s['rank'],status='REVIEW_REQUIRED',issues=[r.get('error','VOICE_QA_FAILED')]);continue
     r=dict(r,script_text=copy_by_rank[s['rank']]['localized_script']['text'],script_language=language)
     write(HOME/'cache/tts'/f'{r["fingerprint"]}.result.json',r)
     from .review_media import base_clips,duration_check
     from .item_review import audit
     rank=s['rank'];self.store.item(id,rank,audio_result=r)
     self.store.done(id,'TTS_DONE',rank,r['fingerprint'],r)
     from .auto_supplement import prepare as prepare_append,adapt as adapt_append
     clips,match,append=prepare_append(self.store.get(id),s,base_clips(s,decisions[rank],effective(job,rank)),r['duration'],effective(job,rank))
     self.store.item(id,rank,duration_match=match,auto_supplement=append)
     if append:self.store.done(id,'AUTO_SUPPLEMENT_PLAN',rank,fingerprint(append),append)
     self.store.done(id,'DURATION_FIT',rank,fingerprint(match),match)
     if not match['fits']:
      self.store.item(id,rank,status='WAITING_SUPPLEMENT_MEDIA',issues=['DURATION_FIT_FAILED'])
      audit(self.store,id,rank,'VISUAL_DURATION_INSUFFICIENT',**match);continue
     s=adapt_append(s,clips,append)
     from .source_library import adapt as adapt_health
     s=adapt_health(s,clips)
     output=output_directory(job)/f'AutoCut_{id[:8]}_{s["rank"]:02}_{language}.mp4'
     try:p=make_plan(s,r,production_copy(copy_by_rank[s['rank']]),decisions[s['rank']],title,work,output,product,job.get('retain_original',False) or bool(effective(job,s['rank']).get('retain_source_title') and analyses[s['rank']].get('only_source_title_overlay')),effective(job,s['rank']),effective_sources(job,s['rank']),target=language);plans.append(p)
     except ValueError as e:self.store.item(id,s['rank'],status='WAITING_SUPPLEMENT_MEDIA' if str(e) in {'DURATION_NEEDS_SUPPLEMENT_REVIEW','REMIX_DURATION_REVIEW'} else 'REVIEW_REQUIRED',issues=[str(e)]);continue
     self.store.done(id,'SUBTITLE',s['rank'],fingerprint(p.get('final_script')),dict(status='COMPLETE'));self.store.item(id,s['rank'],status='EDITING',plan=str(work/'plans'/f'{s["rank"]:02}.json'));self.store.done(id,'TTS_DONE',s['rank'],r['fingerprint'],r)
    self.finish_plans(id,h,job,plans,work)
    changed=[s['filename'] for s in original_sources if sha(s['path'])!=s['sha256']]
    if changed:raise ValueError('SOURCE_HASH_CHANGED')
    items=self.store.get(id)['items'];status='COMPLETED' if items and all(x['status']=='PASS' for x in items) else 'REVIEW_REQUIRED'
    self.store.update(id,state=status,progress=100,elapsed=time.monotonic()-started,source_hash_unchanged=True)
    report=HOME/'reports'/f'{id}.md';report.write_text('# AutoCut\n\n'+json.dumps({'job':id,'product':product['product_name'],'job_name':job.get('job_name',product['product_name']),'mode':job.get('mode','Transport'),'provider_models':models,'state':status,'items':items,'task_instruction':{k:job.get(k) for k in ['custom_instruction_status','resolved_overrides_json','instruction_summary','instruction_driver','instruction_model','instruction_version','instruction_issues']}},ensure_ascii=False,indent=2));h.profiler.report(report)
  except Stopped:self.store.update(id,state='INTERRUPTED',elapsed=time.monotonic()-started)
  except Exception as exc:
   code=exc.code if isinstance(exc,DriverError) else str(exc)
   if not re.fullmatch('[A-Z][A-Z0-9_:,-]{1,100}',code):code='JOB_REVIEW_REQUIRED'
   self.store.update(id,state='REVIEW_REQUIRED',issues=[code],elapsed=time.monotonic()-started)

 def finish_plans(self,id,h,job,plans,work):
  from .automatic_finish import finish
  return finish(self,id,h,job,plans,work)
