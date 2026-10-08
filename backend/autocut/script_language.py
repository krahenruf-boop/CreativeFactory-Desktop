"""Production-language boundary. SOURCE LANGUAGE is never TARGET LANGUAGE.

No network and no source-text fallback. Local checks intentionally fail closed
on long, uncertain text. Metadata disambiguates only short neutral titles.
"""
import copy,re,unicodedata

VERSION='FINAL_LANGUAGE_V1'
MARKETS={'br':'pt-BR','brazil':'pt-BR','巴西':'pt-BR','mx':'es-MX','mexico':'es-MX','墨西哥':'es-MX'}
NAMES={'pt-BR':'Brazilian Portuguese (pt-BR)','es-MX':'Mexican Spanish (es-MX)','en':'English','zh-CN':'Simplified Chinese (zh-CN)'}
def canonical(value):
 value=str(value or '').strip().replace('_','-');base=value.lower().split('-')[0]
 return {'pt':'pt-BR','portuguese':'pt-BR','es':'es-MX','spanish':'es-MX','en':'en','english':'en','zh':'zh-CN','chinese':'zh-CN'}.get(base,value)
def target_language(job):
 market=MARKETS.get(str(job.get('market','')).casefold())
 if market:return market
 preset=job.get('preset_snapshot') or job.get('product_profile') or {}
 language=job.get('target_language') or preset.get('target_language') or job.get('language') or preset.get('language')
 if not language:raise ValueError('TARGET_LANGUAGE_REQUIRED')
 return canonical(language)
def words(text):
 return re.findall(r'[a-z]+',''.join(c for c in unicodedata.normalize('NFKD',str(text).casefold()) if not unicodedata.combining(c)))
VOCAB={
 'en':set('the this that these those you your yours with without is are was were and but have has had not no longer very so easy use used using look looks my me i it its for from into out can could will would need needs carry carrying bag huge stopped every only now buy get makes how why before after feel skin apply eyeshadow makeup waterproof compact beautiful amazing perfect smooth wrinkle wrinkles sale stock hurry dont cant doesnt lets our their they them we'.split()),
 'pt-BR':set('voce voces seu sua seus suas com sem nao uma um e no na nos nas isso esse essa esses essas olha olhe veja aqui fivela cinto calca pode podem mais muito facil fica ficou gente bolso bolsa linda lindo bonita bonito roupa roupas pratico pratica para por que como meu minha agora tambem tem tenho precisa preciso precisar so hoje compre comprar coloque usar sombra pele facil olhos gostou cores oferece oferta desconto aproveite detalhe detalhes sempre maneira acabou carregar carregar anos funciona funciona dura agua'.split()),
 'es-MX':set('tu tus usted ustedes con sin un una el la los las esto este esta estos estas mira miren hebilla cinturon pantalon puedes pueden tienes tiene tengo necesitas necesito necesitas ya deje cargar llevar bolsa enorme ropa practico practica para por que como mi mis ahora tambien hay solo hoy compra comprar pon usar sombra piel ojos bonita bonito lindo linda colores oferta descuento aprovecha detalle detalles siempre manera facil funciona agua queda quedo eres es y muy mas no te se del al lo mejor quieres quieres tan'.split())}
STRONG={
 'pt-BR':set('voce voces seu sua seus suas com sem nao uma um e no na nos nas isso esse essa esses essas olha olhe veja fivela calca pode podem mais muito fica ficou gente meu minha tambem tem tenho precisa preciso so hoje compre coloque olhos oferece aproveite'.split()),
 'es-MX':set('tu tus usted ustedes con sin el la los las esto este estos estas mira miren hebilla cinturon pantalon puedes pueden tienes tiene tengo necesitas necesito ya deje llevar mi mis tambien hay solo hoy compra pon ojos aprovecha eres es y muy mas te del al lo mejor quieres'.split())}
def detect_script_language(text):
 if not isinstance(text,str) or not text.strip():return 'unknown'
 if re.search(r'[\u3400-\u9fff]',text):return 'zh-CN'
 tokens=words(text)
 if not tokens:return 'unknown'
 scores={k:sum(1 for w in tokens if w in v)+sum(2 for w in tokens if w in STRONG.get(k,set())) for k,v in VOCAB.items()}
 # Spanish/Portuguese shared words alone cannot identify either language.
 for k in ('pt-BR','es-MX'):
  if not any(w in STRONG[k] for w in tokens):scores[k]*=.25
 ranked=sorted(scores,key=scores.get,reverse=True);best,second=ranked[:2]
 if scores[best]<1 or scores[best]<scores[second]*1.25:return 'unknown'
 return best
def row(value,language=None,zh=''):
 return copy.deepcopy(value) if isinstance(value,dict) else dict(text=value or '',language=language,text_zh=zh)
def check(value,target,kind='SCRIPT',require_metadata=False):
 r=row(value);text=r.get('text','');target=canonical(target);declared=canonical(r.get('language'));detected=detect_script_language(text)
 code='TITLE_LANGUAGE_MISMATCH' if kind=='TITLE' else 'FINAL_SCRIPT_LANGUAGE_MISMATCH'
 if not isinstance(text,str) or not text.strip():raise ValueError('LOCALIZATION_REQUIRED' if kind=='SCRIPT' else code)
 if require_metadata and not declared:raise ValueError(code)
 if declared and declared!=target:raise ValueError(code)
 if detected!='unknown' and detected!=target:raise ValueError(code)
 if detected=='unknown' and (not declared or len(words(text))>4 or not any(w in VOCAB.get(target,set()) for w in words(text))):raise ValueError(code)
 r.update(language=target,text=text.strip());return r
def script(item):
 if item.get('voiceover_mode')=='MUSIC_ONLY':return None
 if item.get('final_script') is not None:
  return row(item['final_script'],item.get('final_script_language'),item.get('final_script_zh',''))
 return row(item.get('localized_script') or (item.get('copy') or {}).get('localized_script') or {})
def title(item):
 value=item.get('final_title')
 if value is not None:return row(value,item.get('final_title_language'),item.get('title_text_zh',''))
 candidates=item.get('title_candidates') or (item.get('copy') or {}).get('titles',[])
 text=item.get('title')
 chosen=next((c for c in candidates if c.get('text')==text),None) if text else next((c for c in candidates if str(c.get('id'))==str(item.get('selected_title_id'))),None)
 return row(chosen or text or {},item.get('final_title_language'),item.get('title_text_zh',''))
def normalize_response(response,job,strict=False):
 out=copy.deepcopy(response);language=target_language(job)
 out['localized_script']=check(out.get('localized_script',{}),language,require_metadata=strict)
 out['titles']=[check(t,language,'TITLE',strict) for t in out.get('titles',[])]
 return out
def script_fields(response,job,music=False):
 if music:return dict(final_script=None,final_script_language=None,final_script_zh='',localized_script=None,voiceover_mode='MUSIC_ONLY')
 r=check(response.get('localized_script',{}),target_language(job))
 return dict(final_script=r['text'],final_script_language=r['language'],final_script_zh=r.get('text_zh',''),localized_script=r,voiceover_mode='VOICEOVER')
def issues(job,item):
 errors=[];language=target_language(job)
 if item.get('voiceover_mode')!='MUSIC_ONLY':
  candidate=script(item)
  if candidate.get('text'):
   try:check(candidate,language)
   except ValueError:errors.append('LEGACY_SCRIPT_LANGUAGE_MISMATCH' if not item.get('final_language_version') else 'FINAL_SCRIPT_LANGUAGE_MISMATCH')
  elif item.get('analysis',{}).get('clean_script') or item.get('script_status') in {'USER_APPROVED','AUTO'}:errors.append('LOCALIZATION_REQUIRED')
 t=title(item)
 if item.get('title_enabled',True) and t.get('text'):
  try:check(t,language,'TITLE')
  except ValueError:errors.append('TITLE_LANGUAGE_MISMATCH')
 return errors
def subtitle_guard(script_row,language,alignment,audio):
 approved=check(script_row,language);text=approved['text']
 if audio.get('script_text') and audio['script_text']!=text:raise ValueError('SUBTITLE_SCRIPT_MISMATCH')
 actual=''.join(alignment.get('characters',[])) if alignment else ''
 # Alignment must describe the exact spoken manuscript, ignoring punctuation.
 from app.s2.tts_text import prepare_tts_text
 if not actual or actual!=prepare_tts_text(text):raise ValueError('SUBTITLE_SCRIPT_MISMATCH')
 check(dict(text=actual,language=language),language)
 return approved
