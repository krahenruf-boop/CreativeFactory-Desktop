from .script_language import target_language,detect_script_language,check,script,script_fields,VERSION
"""Small task interaction contracts. No media algorithm or provider implementation."""
import copy, hashlib, json, time, uuid
from enum import Enum
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .copy_presentation import TranslationCache, presented_job, approval_guard, chinese
from .provider_models import job_models
from .task_instructions import OverrideValues, resolve, summary, digest

class ScriptMode(str,Enum):
 AUTO='AUTO';TABLE_SELECTED='TABLE_SELECTED';EXISTING='EXISTING';REQUIRE_APPROVAL='REQUIRE_APPROVAL'

TAG_INTENTS={
 'pain':('强痛点',{'title_direction':['pain']}),'benefit':('强卖点',{'title_direction':['benefit']}),
 'emotion':('强情绪',{'title_direction':['emotion'],'copy_style':'emotional'}),
 'hard_sell':('硬广',{'copy_style':'hard_sell','promotion_intensity':'hard_sell'}),
 'structure':('保留原结构',{'preserve_source_structure':True}),
 'fast':('节奏稍快',{'video_pace':'slightly_fast'}),'no_cta':('不加CTA',{'cta_enabled':False}),
 'handled':('水印已处理',{'watermark_mode_override':'handled'}),
 'subtitle_up':('字幕上移',{'subtitle_position_adjustment':'up'}),
 'promotion':('促销强化',{'promotion_intensity':'strong'}),
 'concise':('口播短一点',{'copy_style':'concise'})}

def tags(store):
 return store.settings().get('custom_tags',[dict(id=k,label=v[0],intent=k) for k,v in TAG_INTENTS.items() if k!='concise'])

def save_tags(store,rows):
 if not isinstance(rows,list) or len(rows)>30:raise ValueError('INVALID_REQUEST')
 clean=[]
 for row in rows:
  if set(row)-{'id','label','intent'} or row.get('intent') not in TAG_INTENTS:raise ValueError('INVALID_REQUEST')
  label=str(row.get('label','')).strip()
  if not 1<=len(label)<=24:raise ValueError('INVALID_REQUEST')
  clean.append(dict(id=str(row.get('id') or uuid.uuid4().hex),label=label,intent=row['intent']))
 if len({r['id'] for r in clean})!=len(clean):raise ValueError('INVALID_REQUEST')
 store.set_setting('custom_tags',clean);return clean

def merge_tags(snapshot,ids,store,context):
 if not ids:return snapshot
 lookup={t['id']:t for t in tags(store)};values={};chosen=[]
 for id in ids:
  if id not in lookup:raise ValueError('INVALID_REQUEST')
  tag=lookup[id];chosen.append(copy.deepcopy(tag))
  for k,v in TAG_INTENTS[tag['intent']][1].items():
   if k=='title_direction':values[k]=list(dict.fromkeys(values.get(k,[])+v))
   else:values[k]=v
 original=copy.deepcopy(snapshot.get('resolved_overrides_json') or {})
 # Explicit free-text interpretation wins over shortcut defaults, then per-item wins.
 values.update(original.get('values',{}));original['values']=values
 guarded=resolve({'overrides':original},snapshot.get('custom_instruction_raw',''),context)
 result=dict(snapshot,**{k:guarded[k] for k in ['resolved_overrides_json','instruction_summary','custom_instruction_status']})
 result['instruction_issues']=list(dict.fromkeys(snapshot.get('instruction_issues',[])+guarded['instruction_issues']))
 result['task_tag_snapshot']=chosen
 return result

class Preset(BaseModel):
 model_config=ConfigDict(extra='forbid')
 id:str|None=None
 product_name:str=Field(min_length=1,max_length=100)
 market:Literal['Brazil','Mexico']='Brazil'
 language:Literal['pt-BR','es-MX']='pt-BR'
 default_mode:Literal['Transport','Remix']='Transport'
 script_mode:Literal['AUTO','REQUIRE_APPROVAL']='AUTO'
 script_reference_mode:Literal['smart','prefer_original']='smart'
 title_approval_mode:Literal['auto','manual']='auto'
 watermark_mode:Literal['auto','handled']='auto'
 title_visual:str='TIKTOK_BR_TRANSPORT_VISUAL_V1'
 subtitle_visual:str='TIKTOK_BR_TRANSPORT_VISUAL_V1'
 overlay_strategy:Literal['HEADLESS_OVERLAY_STANDARD_V1']='HEADLESS_OVERLAY_STANDARD_V1'
 voice_preferences:dict[str,str]=Field(default_factory=dict)
 task_defaults:OverrideValues=Field(default_factory=OverrideValues)

def save_preset(store,payload):
 p=Preset.model_validate(payload).model_dump(mode='json',exclude_none=True)
 if p['language']!=('es-MX' if p['market']=='Mexico' else 'pt-BR'):raise ValueError('INVALID_REQUEST')
 if p['title_visual']!='TIKTOK_BR_TRANSPORT_VISUAL_V1' or p['subtitle_visual']!='TIKTOK_BR_TRANSPORT_VISUAL_V1':raise ValueError('INVALID_REQUEST')
 if set(p['voice_preferences'])-{'female','male'} or any(len(v)>100 for v in p['voice_preferences'].values()):raise ValueError('INVALID_REQUEST')
 old=store.product(p.get('id')) or {};p['usage_scope']=old.get('usage_scope','production')
 p['task_defaults']=resolve({'overrides':{'values':p['task_defaults']}},'',dict(mode=p['default_mode'],usage_scope=p['usage_scope']))['resolved_overrides_json']['values']
 # Only user profile records change. Bundled visual and overlay rules remain frozen.
 return store.save_product(dict(old,**p))

def table_scripts(reference,market):
 language='es-MX' if market=='Mexico' else 'pt-BR';rows=[]
 for idx,row in enumerate(reference.get('rows',[]),1):
  def cell(keys,exclude=()):
   return next((str(v).strip() for k,v in row.items() if not any(x in k.lower() for x in exclude) and any(x in k.lower() for x in keys) and str(v).strip()),'')
  zh_keys=['中文','zh-cn','zh_cn','chinese','translation_zh','text_zh']
  foreign=[(language,['pt-br','葡语','葡萄牙语'])] if market=='Brazil' else [(language,['es-mx','西语','西班牙语'])]
  foreign += [('en',['english','英文']),('pt-BR',['pt-br','葡语','葡萄牙语']),('es-MX',['es-mx','西语','西班牙语'])]
  text='';lang='unknown'
  for code,keys in foreign:
   text=cell(keys,zh_keys) or next((str(v).strip() for k,v in row.items() if k.strip().lower()==code.split('-')[0].lower() and str(v).strip()),'')
   if text:lang=code;break
  if not text:text=cell(['文案内容','口播','script','文案','content'],[*zh_keys,'编号','id','标题','title','备注','notes'])
  zh=cell(zh_keys)
  if not text and zh:text=zh;lang='zh-CN'
  if not text:continue
  code=cell(['文案编号','编号','script_id','id']) or f'R{idx}'
  rows.append(dict(id=digest([idx,row])[:24],reference_id=code,text=text,text_zh=chinese(zh),language=lang,performance=cell(['出单','表现','performance','orders']),notes=cell(['备注','notes']),source=reference.get('filename',''),title=text[:90]))
 return rows

def existing_scripts(store,product,market,preset_id=None):
 rows=[];seen=set()
 for j in store.jobs():
  if j.get('market')!=market or j.get('product_name')!=product:continue
  raw=j;j=presented_job(j,store.home,job_models(j)['deepseek'])
  for item in j['items']:
   if item.get('script_candidates') and not j.get('scripts_approved') and j.get('script_mode')=='REQUIRE_APPROVAL':continue
   original=next(x for x in raw['items'] if x['rank']==item['rank']);candidate=script(original) or {};text=candidate.get('text','')
   if not text or digest(text) in seen:continue
   seen.add(digest(text));rows.append(dict(id=j['id']+':'+item['rank']+':'+digest(text)[:12],text=text,text_zh=item.get('script_text_zh',''),language=detect_script_language(text),localization_status='READY' if detect_script_language(text)==target_language(j) else 'NEEDS_LOCALIZATION',market=market,source=j.get('job_name',j['product_name']),used=j['updated'],title=text[:90]))
 return rows

def script_snapshot(data,store):
 mode=ScriptMode(data.get('script_mode','AUTO'));data['script_mode']=mode.value
 if mode==ScriptMode.TABLE_SELECTED:
  rows=table_scripts(data.get('reference',{}),data['market']);selected=next((r for r in rows if r['id']==data.get('script_reference_id')),None)
  if not selected:raise ValueError('SCRIPT_SELECTION_INVALID')
  data['reference_file_id']=digest(data['reference']);data['script_snapshot']=TranslationCache(store.home).row(selected,store.settings()['deepseek_model'])
 elif mode==ScriptMode.EXISTING:
  rows=existing_scripts(store,data['product_name'],data['market'],data['product_profile_id']);selected=next((r for r in rows if r['id']==data.get('existing_script_id')),None)
  if not selected:raise ValueError('SCRIPT_SELECTION_INVALID')
  data['script_snapshot']=TranslationCache(store.home).row(selected,store.settings()['deepseek_model'])
 return data

def selected_context(c,job):
 c=copy.deepcopy(c);script=job.get('script_snapshot')
 if script:
  c['clean_script']=script['text'];c['source_script']=script['text'];c['source_language']=script.get('language') or 'unknown'
  c['video_context']+='\nUse the user-selected script as the primary copy. Localize only as needed for the market, visual facts and duration; do not replace its structure. Selected reference: '+json.dumps(script,ensure_ascii=False)[:3500]
  c['script_group_id']=digest([c['script_group_id'],script])
 return c

def approval_contexts(contexts,job):
 # Content confirmation starts with the best localized source script.
 return contexts

def copy_candidates(copies,group):
 out=[];seen=set()
 for key in [group,group+':B',group+':C']:
  response=copies.get(key,{}).get('response');text=(response or {}).get('localized_script',{}).get('text','')
  if not text or digest(text) in seen:continue
  seen.add(digest(text));row=dict(id=digest(response)[:24],text=text,language=response['localized_script'].get('language'),response=copy.deepcopy(response))
  if 'text_zh' in response.get('localized_script',{}):row['text_zh']=chinese(response['localized_script']['text_zh'])
  out.append(row)
 return out

def approve_scripts(store,id,selections,allow_original=False,ranks=None):
 job=store.get(id)
 from .ownership import require_action
 if ranks is None:ranks=[x['rank'] for x in job['items'] if x.get('script_status')=='WAITING_APPROVAL']
 for rank in ranks:require_action(store,id,rank,'APPROVE_SCRIPT')
 bypass=approval_guard(presented_job(dict(job,items=[x for x in job['items'] if x['rank'] in ranks]),store.home,job_models(job)['deepseek']),'script',allow_original)
 chosen={}
 for item in job['items']:
  if item['rank'] not in ranks:continue
  candidates=item.get('script_candidates',[])
  if not candidates:continue
  selected=selections.get(item['rank'],candidates[0]['id']);row=next((r for r in candidates if r['id']==selected),None)
  if not row:raise ValueError('SCRIPT_SELECTION_INVALID')
  check(row,target_language(job));check(row['response']['localized_script'],target_language(job))
  chosen[item['rank']]=dict(id=selected,text=row['text'],response=copy.deepcopy(row['response']),candidate_sha=digest(candidates))
 if not chosen:raise ValueError('SCRIPT_SELECTION_INVALID')
 for rank,row in chosen.items():
  store.done(id,'FINAL_SCRIPT_LANGUAGE_CHECK',rank,digest(row['text']),dict(status='PASS',target_language=target_language(job)));store.done(id,'SCRIPT_APPROVED',rank,row['candidate_sha'],row);store.item(id,rank,status='COPY_READY',copy=row['response'],script_status='USER_APPROVED',selected_script_id=row['id'],final_language_version=VERSION,**script_fields(row['response'],job))
 store.update(id,script_selections=dict(job.get('script_selections',{}),**chosen),script_zh_bypass=bypass,scripts_approved_at=time.time())
 return chosen
