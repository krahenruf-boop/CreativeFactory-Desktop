"""One user decision, two stage approvals. Never a final-production approval.

All validation precedes an atomic SQLite snapshot/checkpoint commit. Only the
current rank is resumed. Existing language, final-output and stale-action guards
remain authoritative; provider algorithms and shared caches are unchanged.
"""
import copy,json,time
from pydantic import Field
from .item_review import Strict,Bilingual,get_item,audit
from .ownership import require_action,item_state,digest
from .script_language import script,check,script_fields,target_language,detect_script_language,NAMES,VERSION
from .copy_presentation import chinese
from .provider_models import job_models

ERRORS={'CONTENT_NOT_READY':'脚本还未准备完成，请更换脚本或稍后重试','CONTENT_TITLE_REQUIRED':'请选择一个可用标题','CONTENT_TRANSLATION_REQUIRED':'中文对照暂不可用，请重试','TITLE_GENERATION_FAILED':'标题生成失败，原有标题已保留','CONTENT_BUSY':'正在准备内容，请稍后','CUSTOM_TITLE_INVALID':'请输入标题（最多 300 字）'}
class Titles(Strict):titles:list[Bilingual]=Field(min_length=3,max_length=3)
class CustomTitle(Strict):title:Bilingual

def content_view(j,x,cache=None):
 d=x.get('script_draft')
 if not d or d.get('id')==x.get('approved_draft_id'):d=None
 music=(d or {}).get('mode')=='MUSIC_ONLY' if d else x.get('voiceover_mode')=='MUSIC_ONLY'
 spoken=copy.deepcopy((d.get('candidates') or [{}])[0] if d else script(x) or {})
 # Legacy script gates may have only candidates. Never fall back to ASR text.
 if not spoken.get('text') and not d and x.get('script_candidates'):spoken=copy.deepcopy(x['script_candidates'][0])
 titles=copy.deepcopy(x.get('content_titles') or ((d or {}).get('titles') if (d or {}).get('mode')=='LOCALIZATION' else None) or x.get('title_candidates') or (x.get('copy') or {}).get('titles') or (d or {}).get('titles') or [])[:3]
 custom=copy.deepcopy(x.get('content_custom_title'))
 if cache:
  model=job_models(j)['deepseek'];spoken=cache.row(spoken,model)
  titles=[cache.row(t,model) for t in titles]
  if custom:custom=cache.row(custom,model)
 return dict(script=None if music else spoken,voiceover_mode='MUSIC_ONLY' if music else 'VOICEOVER',titles=titles,custom_title=custom,default_title_id=(x.get('selected_title_id') if x.get('content_edit') and any(str(t.get('id'))==str(x.get('selected_title_id')) for t in titles) else (titles[0].get('id') if titles else None)),source=(d or {}).get('source') or x.get('script_source'),draft_id=(d or {}).get('id'),title_generation=x.get('title_generation',0))

class ContentConfirmation:
 def __init__(self,store,manager,review,translations):self.store=store;self.manager=manager;self.review=review;self.translations=translations
 def begin(self,id,rank,token):
  with self.store.lock:
   j,x,_=require_action(self.store,id,rank,'EDIT_CONTENT',token)
   self.store.item(id,rank,status='WAITING_SCRIPT_APPROVAL',content_edit=True,script_candidates=x.get('script_candidates') or ([dict(script(x),id='current')] if script(x) else []))
  return get_item(self.store,id,rank)[1]
 def title_request(self,j,x,text=None):
  language=target_language(j);custom=text is not None;schema=CustomTitle if custom else Titles
  prompt=('Localize one custom video title into the target market language. If input is already in that language, keep it exactly (trim whitespace only). ' if custom else 'Generate exactly three distinct compact TikTok video titles for THIS item only. A is recommended. Return no script. Use a fresh angle compared to previous titles. ')
  prompt+='OUTPUT LANGUAGE: '+NAMES.get(language,language)+'. Return language, text, text_zh for each title. text_zh is a faithful, natural Chinese comparison, with no new claims. Never put Chinese in target text. Return JSON matching: '+json.dumps(schema.model_json_schema(),ensure_ascii=False)
  context=dict(target_language=language,product=j.get('product_name'),script=(content_view(j,x)['script'] or {}).get('text',''),analysis=x.get('analysis',{}),previous_titles=content_view(j,x)['titles'],generation=x.get('title_generation',0)+1)
  if custom:context['input_title']=text
  adapter=self.translations.adapter()
  try:out=adapter.complete(job_models(j)['deepseek'],[dict(role='system',content=prompt),dict(role='user',content=json.dumps(context,ensure_ascii=False))],max_tokens=2400)['data']
  finally:adapter.close()
  made=schema.model_validate(out).model_dump();rows=[made['title']] if custom else made['titles']
  for t in rows:
   if custom and detect_script_language(text)==language:t['text']=text.strip()
   check(t,language,'TITLE',require_metadata=True)
   if not chinese(t.get('text_zh')):raise ValueError('CONTENT_TRANSLATION_REQUIRED')
  if not custom and len({r['text'].casefold() for r in rows})!=3:raise ValueError('TITLE_GENERATION_FAILED')
  return [dict(t,id=4 if custom else i,angle='custom' if custom else 'content') for i,t in enumerate(rows,1)]
 def titles(self,id,rank,token,text=None):
  if text is not None:
   if not isinstance(text,str) or not text.strip() or len(text)>300:raise ValueError('CUSTOM_TITLE_INVALID')
   text=text.strip()
  # Mark busy before releasing the store lock: concurrent refresh/confirm is rejected.
  with self.store.lock:
   j,x,_=require_action(self.store,id,rank,'EDIT_CONTENT_TITLES',token)
   self.store.item(id,rank,status='SCRIPTING',operation_inflight=True)
  try:
   rows=self.title_request(j,x,text)
   # Cache must succeed before changing the visible title generation.
   for t in rows:self.translations.cache.put(t['text'],target_language(j),job_models(j)['deepseek'],t['text_zh'])
   with self.store.lock:
    latest=get_item(self.store,id,rank)[1]
    if not latest.get('operation_inflight'):raise ValueError('STALE_ACTION')
    fields=dict(status=x['status'],operation_inflight=False)
    if text is None:fields.update(content_titles=rows,title_generation=x.get('title_generation',0)+1,batch_selected_title_id=rows[0]['id'])
    else:fields.update(content_custom_title=dict(rows[0],original_text=text),batch_selected_title_id=4)
    self.store.item(id,rank,**fields)
  except Exception as e:
   self.store.item(id,rank,status=x['status'],operation_inflight=False)
   if str(e)=='STALE_ACTION':raise
   raise ValueError('TITLE_GENERATION_FAILED') from e
  return get_item(self.store,id,rank)[1]
 def approve(self,id,rank,data):
  with self.store.lock:
   j,x,_=require_action(self.store,id,rank,'APPROVE_CONTENT',data.get('action_token'))
   c=content_view(j,x,self.translations.cache);music=c['voiceover_mode']=='MUSIC_ONLY';language=target_language(j);d=x.get('script_draft') if c['draft_id'] else None
   if d and d.get('base_revision')!=x.get('script_revision',0):raise ValueError('STALE_ACTION')
   spoken=dict(text='',text_zh='',language=language) if music else check(c['script'] or {},language)
   if not music and not chinese(spoken.get('text_zh')):raise ValueError('CONTENT_TRANSLATION_REQUIRED')
   title=next((t for t in c['titles']+([c['custom_title']] if c['custom_title'] else []) if str(t.get('id'))==str(data.get('title_id'))),None)
   if not title:raise ValueError('CONTENT_TITLE_REQUIRED')
   title=check(title,language,'TITLE',require_metadata=True)
   if not chinese(title.get('text_zh')):raise ValueError('CONTENT_TRANSLATION_REQUIRED')
   spoken={k:spoken[k] for k in ('text','text_zh','language')}
   def title_snapshot(t):return {k:t[k] for k in ('id','text','text_zh','language','angle') if k in t}
   title=title_snapshot(title)
   old=script(x) or {};changed=(old.get('text') or '')!=spoken['text'] or (x.get('voiceover_mode')=='MUSIC_ONLY')!=music or music and d and d.get('music')!=x.get('music')
   response=copy.deepcopy(x.get('copy') or {});response['localized_script']=spoken
   response['titles']=[title_snapshot(t) for t in c['titles']+([c['custom_title']] if title['id']==4 else [])]
   fields=dict(copy=response,**script_fields(response,j,music),final_language_version=VERSION,script_status='USER_APPROVED',title_status='USER_APPROVED',title=title['text'],final_title=title,final_title_language=language,title_text_zh=title['text_zh'],selected_title_id=title['id'],title_candidates=response['titles'],content_titles=None,content_edit=False,content_confirmed=True,content_confirmed_at=time.time(),status='COPY_READY',issues=[],previous_output=x.get('output') or x.get('previous_output'),output=None,plan=None)
   if d:
    fields.update(approved_draft_id=d['id'],script_source=d['source'],music=d.get('music'),script_revision=x.get('script_revision',0)+int(bool(changed)))
    if d['mode']=='PASTED':fields.update(pasted_original_text=d['original_text'],pasted_original_language=d['original_language'])
    if d['mode'] in {'PASTED','TABLE'}:fields.update(source_script=d['original_text'],clean_source_script=d['original_text'],source_language=d['original_language'])
   if changed:fields.update(audio_result=None,voice=None,decision=None,duration_match=None,supplement_plan=None,script_candidates=[] if music else [dict(spoken,id='current',response=response)])
   invalid=['FINAL_RENDERED','QA_DONE','FINAL_APPROVED']+(['TTS_DONE','ALIGNMENT','SUBTITLE','OVERLAY_LOCKED','OVERLAY_APPROVED'] if changed else [])
   row=dict(x,**fields,action_revision=x.get('action_revision',0)+1)
   # Atomic persistence: no state where only one approval has committed.
   with self.store.connect() as db:
    db.execute('BEGIN IMMEDIATE')
    db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',(json.dumps(row,ensure_ascii=False),id,x['rank']))
    db.execute('DELETE FROM checkpoints WHERE job_id=? AND item IN (?,?) AND stage IN ('+','.join('?' for _ in invalid)+')',(id,str(int(rank)),x['rank'],*invalid))
    for stage,value in [('FINAL_SCRIPT_LANGUAGE_CHECK',dict(status='EXEMPT_MUSIC_ONLY' if music else 'PASS',language=language)),('SCRIPT_APPROVED',spoken),('TITLE_APPROVED',title)]:
     db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)',(id,stage,str(rank),digest(value),json.dumps(value,ensure_ascii=False)))
   self.manager.enqueue_item(id,rank,'SCRIPT' if changed else 'TITLE')
  audit(self.store,id,rank,'CONTENT_APPROVED',script_changed=bool(changed),title=title,invalidated=invalid,old_output_preserved=fields['previous_output'])
  return get_item(self.store,id,rank)[1]
