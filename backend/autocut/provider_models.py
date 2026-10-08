"""Small, explicit model menu. No keys or model decisions in the web client.

Directory/API contracts checked 2026-09-30. Keep older jobs on their original
models; only a newly created job snapshots the current defaults.
"""
import copy

MODEL_OPTIONS = {
 'deepseek': [
  {'id':'deepseek-flash','name':'DeepSeek Flash','hint':'文案与标题 · 默认'},
  {'id':'deepseek-v4-pro','name':'DeepSeek V4 Pro','hint':'文案与标题'},
 ],
 'elevenlabs': [
  {'id':'eleven_flash_v2_5','name':'Eleven Flash v2.5','hint':'快速配音 · 默认'},
  {'id':'eleven_multilingual_v2','name':'Eleven Multilingual v2','hint':'多语言配音'},
  {'id':'eleven_turbo_v2_5','name':'Eleven Turbo v2.5','hint':'快速配音'},
  {'id':'eleven_v3','name':'Eleven v3','hint':'表现力配音'},
  {'id':'eleven_v4','name':'Eleven v4','hint':'新一代配音'},
  {'id':'eleven_v4_turbo','name':'Eleven v4 Turbo','hint':'新一代快速配音'},
 ],
}
MODEL_DEFAULTS={'deepseek':'deepseek-flash','elevenlabs':'eleven_flash_v2_5'}
LEGACY_MODELS={'deepseek':'deepseek-chat','elevenlabs':'eleven_flash_v2_5'}

def validate_model(provider,value):
 if value not in {m['id'] for m in MODEL_OPTIONS[provider]}:
  raise ValueError('PROVIDER_MODEL_UNSUPPORTED')
 return value

def selected_models(settings):
 return {p:validate_model(p,settings.get(p+'_model',default)) for p,default in MODEL_DEFAULTS.items()}

def job_models(job):
 # Never use today's defaults when resuming a job created by an older app.
 return dict(LEGACY_MODELS,**job.get('provider_models',{}))

def tts_payload(text,voice_id,model_id,catalog,language):
 from .script_language import check
 check(dict(text=text),language)
 from app.s2.engine import Engine
 from app.s2.types import TTS
 cat=copy.deepcopy(catalog)
 model=next((m for m in cat['models'] if m['id']==model_id),None)
 if not model:raise ValueError('PROVIDER_MODEL_UNAVAILABLE')
 languages={x.get('language_id') for x in model.get('languages',[])}
 if languages and language.split('-')[0] not in languages:
  raise ValueError('PROVIDER_MODEL_LANGUAGE_UNSUPPORTED')
 # V4 has Stability + Similarity, no Speed/Style/Speaker Boost. V3 only
 # exposes Stability here. Do not send unsupported legacy voice controls.
 if model_id in {'eleven_v4','eleven_v4_turbo','eleven_v3'}:
  model['supported']={k:k in ({'stability'} if model_id=='eleven_v3' else {'stability','similarity_boost'}) for k in ['speed','stability','similarity_boost','style','use_speaker_boost']}
 settings={k:v for k,v in {'speed':1.,'stability':.5,'similarity_boost':.75}.items() if model['supported'].get(k)}
 return Engine.tts_payload(None,TTS(text=text,voice_id=voice_id,model_id=model_id,settings=settings),cat)
