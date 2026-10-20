"""Packaging production policy over the existing renderer; one bounded repair."""
import copy,time
from pathlib import Path
from .production_policy import classify,LAYOUT_REPAIR,RENDER_REPAIR,VERSION
from .ownership import final_ready
from .script_language import check,target_language
from .original_text_cover import review_issues as text_cleanup_issues
from .cta_policy import ensure_plan,retain_source_arrow,verify_or_repair,sprite
from app.s3.headless_runtime import write,sha,read
from app.s3.headless_media import preflight_job,approve_preflight,final_job,identity,batch_sheets
from app.s3.visual_preset import build_visual_plan
from app.s3.overlay_policy import lock_plan
from scripts.headless_transport import qa_job


def result(call):
 try:return call().result()
 except Exception as e:return dict(status='FAIL',error=getattr(e,'code',str(e)))

def repaired_visual(plan):
 old=plan['visual'];fresh=build_visual_plan(plan['target_duration'],plan['title'],old['subtitle']['captions'],asset=old['cta']['asset'])
 for k in ('task_rules','user_revision'):
  if k in old:fresh[k]=copy.deepcopy(old[k])
 for k in ('text','end'):fresh['title'][k]=old['title'][k]
 if plan.get('title_enabled',True):
  from .title_layout import apply as fit_title
  fit_title(fresh,plan['title'],max_lines=(plan.get('task_overrides') or {}).get('title_max_lines'))
 fresh['subtitle']['font_size']=old['subtitle']['font_size'];fresh['cta']['enabled']=old['cta'].get('enabled',True)
 return fresh

def finish(manager,id,h,job,plans,work):
 from .pipeline import preflight_review_context
 from .item_review import get_item
 store=manager.store;banks=[]
 for original in plans:
  p=copy.deepcopy(original);rank=p['rank'];repair_used=bool(get_item(store,id,rank)[1].get('auto_repair_used'));warnings=list(get_item(store,id,rank)[1].get('qa_warnings') or [])
  def repair(reason,layout=False):
   nonlocal repair_used,p
   if repair_used:return False
   repair_used=True;store.item(id,rank,auto_repair_used=True,auto_repair_reason=reason)
   if layout:
    # Reuse the frozen preset's proven asset layout; never resize/redesign CTA.
    fresh=repaired_visual(p)
    p['visual']=fresh
    from .source_title_overlay import bind as bind_source_title
    bind_source_title(p,work)
    ensure_plan(p);retain_source_arrow(p,work/'cta_source'/rank);lock_plan(p);write(work/'plans'/f'{rank}.json',p)
   else:
    # Preserve an unusable export as diagnostic evidence, never touch sources.
    out=Path(p['output'])
    if out.exists():
     archive=work/'failed_outputs';archive.mkdir(exist_ok=True);out.rename(archive/(str(time.time_ns())+'-'+out.name))
   return True
  try:
   manager.check(id);lang=target_language(job)
   ensure_plan(p);retain_source_arrow(p,work/'cta_source'/rank);lock_plan(p);write(work/'plans'/f'{rank}.json',p)
   if p.get('voiceover_mode')!='MUSIC_ONLY':check(dict(text=p.get('final_script'),language=p.get('final_script_language')),lang)
   if p.get('title_enabled',True):check(dict(text=p.get('title'),language=p.get('final_title_language')),lang,'TITLE')
   while True:
    store.item(id,rank,status='OVERLAY');r=result(lambda:preflight_job(h,p))
    if r.get('error'):
     if r['error'] in LAYOUT_REPAIR and repair(r['error'],True):warnings.append(r['error']);continue
     raise ValueError(r['error'])
    review=manager.judge(id,h,'VISUAL_QA','preflight-'+rank,preflight_review_context(p,r,job.get('retain_original',False)),'review_overlay',[r['contact_sheet']]);v=classify(review)
    text_issues=text_cleanup_issues(p,review)
    if text_issues:
     store.done(id,'QA_DONE',rank,identity(p),dict(visual=review,status='FAIL',issues=text_issues))
     raise ValueError(text_issues[0])
    if v['status']=='FAIL':
     if repair('SEVERE_VISUAL_QA',True):continue
     raise ValueError('SEVERE_VISUAL_QA')
    warnings+=v['issues']
    approve_preflight(h,p,dict(reviewer='Creative Factory automatic QA',review_contract='AUTOCUT_AUTO_QA_V2',status='PASS',policy_status=v['status'],policy_evidence=v,checks={k:review[k] for k in ['title','subtitle','cta','overlay','source_text','watermark']},preview_sha256=r['output_sha256']))
    store.done(id,'OVERLAY_LOCKED',rank,identity(p),v);break
   manager.check(id);store.item(id,rank,status='RENDERING');store.update(id,stage='导出')
   rendered=result(lambda:final_job(h,p,**({'rework_reason':'FULL_DURATION_TIMING'} if repair_used else {})));reworked=False
   if rendered.get('error') and rendered['error'] in RENDER_REPAIR and repair(rendered['error']):
    reworked=True;rendered=result(lambda:final_job(h,p,rework_reason='ENCODE_CORRUPTION'))
   if rendered.get('error'):raise ValueError(rendered['error'])
   store.item(id,rank,status='CHECKING',output=p['output'],duration=p['target_duration']);store.done(id,'FINAL_RENDERED',rank,identity(p),rendered)
   manager.check(id);technical=result(lambda:qa_job(h,p));tv=classify(technical,True)
   if tv['status']=='FAIL' and repair('FULL_DECODE_FAILURE'):
    rendered=result(lambda:final_job(h,p,rework_reason='FULL_DECODE_FAILURE'))
    if rendered.get('error'):raise ValueError(rendered['error'])
    store.done(id,'FINAL_RENDERED',rank,identity(p),rendered);technical=result(lambda:qa_job(h,p));tv=classify(technical,True)
   if tv['status']=='FAIL':
    store.done(id,'QA_DONE',rank,identity(p),dict(technical=technical,status='FAIL'));raise ValueError('OUTPUT_QA_FAILED')
   # Inspect MP4 pixels, independent of model/config claims. One CTA-only repair
   # never reaches ASR, copy, speech synthesis or subtitle generation.
   base=Path(p['output']);destination=base.with_name(base.stem+'_cta_'+sha(base)[:8]+'.mp4')
   evidence=read(rendered['evidence'])
   # Match only visible arrow pixels; keep the exact subtitle rasters above
   # CTA during a targeted overlay repair too. Do not alter the render identity.
   verification_plan=copy.deepcopy(p)
   verification_plan['cta_preserve_text_layers']=[l for l in evidence.get('text_layers',[]) if l['start']<p['target_duration'] and l['end']>max(0,p['target_duration']-5)]
   arrow=verify_or_repair(verification_plan,base,work/'cta_qa'/rank,destination,not get_item(store,id,rank)[1].get('cta_repair_used'))
   if arrow['repaired']:
    evidence=read(rendered['evidence']);evidence['cta']=sprite(p,work/'cta_qa'/rank/'repair')
    evidence['cta']['horizontal_drift_px']=0
    p['output']=arrow['output'];p['cta_overlay_from']=arrow['cta_overlay_from'];lock_plan(p);write(work/'plans'/f'{rank}.json',p)
    evidence_path=work/'render'/identity(p)/'visual_evidence.json';write(evidence_path,evidence)
    rendered=dict(rendered,output=p['output'],output_sha256=arrow['output_sha256'],evidence=str(evidence_path))
    store.item(id,rank,output=p['output'],cta_repair_used=True,cta_previous_output=str(base))
    technical=result(lambda:qa_job(h,p))
    if technical.get('status')!='PASS':raise ValueError('CTA_RENDER_FAILED')
   rendered['cta_verification']=arrow['cta_verification'];store.done(id,'FINAL_RENDERED',rank,identity(p),rendered)
   from .qa_evidence import review_images
   sheet=work/'qa'/rank/'contact.jpg';images,coverage=review_images(work,rank,p,technical)
   review=manager.judge(id,h,'VISUAL_QA','final-'+rank,dict(plan=p,technical_issues=technical.get('issues',[]),output_sha256=sha(p['output']),retained_source=job.get('retain_original',False),evidence_coverage=coverage),'review_final',images);v=classify(review)
   text_issues=text_cleanup_issues(p,review)
   if text_issues:
    store.done(id,'QA_DONE',rank,identity(p),dict(technical=technical,visual=review,status='FAIL',issues=text_issues))
    raise ValueError(text_issues[0])
   if v['status']=='FAIL':
    # One explicit bounded layout repair, then verify again. Never generic rerun.
    if not repair_used:
     prior=Path(p['output']);archive=work/'failed_outputs';archive.mkdir(exist_ok=True);prior.rename(archive/(str(time.time_ns())+'-'+prior.name))
     repair('SEVERE_VISUAL_QA',True)
     finish(manager,id,h,job,[p],work);continue
    store.done(id,'QA_DONE',rank,identity(p),dict(technical=technical,visual=review,status='FAIL'));raise ValueError('SEVERE_VISUAL_QA')
   warnings=list(dict.fromkeys(warnings+v['issues']));qa_status='WARNING' if warnings or v['status']=='WARNING' else 'PASS'
   store.done(id,'QA_DONE',rank,identity(p),dict(technical=technical,visual=review,status=qa_status,policy=VERSION,warnings=warnings,cta=arrow['cta_verification']))
   store.item(id,rank,qa_status=qa_status,qa_warnings=warnings,issues=[],review_reason=None,thumbnail=str(sheet),source_hash_unchanged=sha(p['source']['path'])==p['source']['sha256'])
   latest,x=get_item(store,id,rank)
   if not final_ready(store,latest,x):raise ValueError('FINAL_NOT_READY')
   store.item(id,rank,status='PASS',production_policy=VERSION);banks.append(technical)
  except Exception as e:
   from .pipeline import Stopped
   if isinstance(e,Stopped):raise
   import re
   code=getattr(e,'code',str(e));code=code if re.fullmatch('[A-Z][A-Z0-9_:,-]{1,160}',code) else 'RENDER_FAILED'
   store.item(id,rank,status='FAILED',qa_status='FAIL',issues=[code],qa_warnings=warnings,production_policy=VERSION)
 if banks:batch_sheets(banks,[],work/'contact_sheets')
