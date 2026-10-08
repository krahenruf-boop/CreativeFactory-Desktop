"""Bilingual presentation contract; mocked providers, real Store/API/plan builder."""
import copy,json,tempfile,unittest,threading
from pathlib import Path
from unittest.mock import patch,MagicMock
from concurrent.futures import Future
from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.server import create_app
from autocut.copy_presentation import *
from autocut.workbench import copy_candidates
from autocut.pipeline import Manager,make_plan
from autocut.provider_models import tts_payload
from app.s3.deepseek_batch import generate_batch
from app.s3.deepseek_copy import prepare,validation
from app.s3.headless_runtime import write,read,sha

BR='Olha como esta fivela deixa o seu look mais prático. Você pode combinar com a sua roupa.'
MX='Mira esta hebilla compacta para combinar con tu ropa. Es un detalle práctico para tu día.'
ZH='看看这个带扣，搭配衣服很方便。'
def response(language='pt-BR',zh=True):
 texts=['Um detalhe no seu look','Prático para combinar com roupa','Olha só esta fivela'] if language=='pt-BR' else ['Un detalle en tu look','Práctica para combinar con ropa','Mira qué linda hebilla']
 r=dict(localized_script=dict(text=BR if language=='pt-BR' else MX,language=language,estimated_style='ugc_tiktok',notes=[]),titles=[dict(id=i+1,text=t,angle=a,language=language) for i,(t,a) in enumerate(zip(texts,['pain_hook','product_benefit','emotion']))])
 if zh:
  r['localized_script']['text_zh']=ZH
  for t,z in zip(r['titles'],['造型中的一个细节','搭配衣服很方便','看看这个带扣']):t['text_zh']=z
 return r
def context(language='pt-BR'):
 return dict(script_group_id=language,dedupe_reviewed=True,source_language='en',source_script='A compact buckle',clean_script='A compact buckle',video_context='A buckle with clothes',product='Buckle',supported_selling_points=['compact'],allowed_claims=['compact'],blocked_claims=[],promotion_context=None,target_duration_seconds=30,market='Brazil' if language=='pt-BR' else 'Mexico',language=language,title_count=3)
class MockAdapter:
 def __init__(self):self.calls=[]
 def complete(self,model,messages,**kwargs):
  self.calls.append((model,messages));rows=json.loads(messages[-1]['content'])['items']
  if 'context' in rows[0]:return {'data':{'items':[dict(id=r['id'],result=response(r['context']['language'])) for r in rows]}}
  return {'data':{'items':[dict(id=r['id'],text_zh='中文对照：'+ZH) for r in rows]}}
 def close(self):pass
class Queue:
 def enqueue_item(self,*args):pass
 def enqueue(self,id):pass
 def stop(self,id):pass
class ComparisonTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'data');self.app=create_app(self.store,None,None,Queue(),'test');self.c=TestClient(self.app,base_url='http://127.0.0.1:9000',headers={'Authorization':'Bearer test'});self.adapter=MockAdapter();self.app.state.translations.factory=lambda:self.adapter
 def tearDown(self):self.tmp.cleanup()
 def job(self,**kw):
  return self.store.create(dict(request_id=digest(kw),input_path=str(self.root),product_name='腰带扣',product_profile_id='general',market='Brazil',mode='Transport',script_mode='AUTO',title_approval_mode='auto',watermark_mode='handled',**kw))[0]
 def test_br_mx_single_batch_response_and_cache_all_models(self):
  for model in ['deepseek-flash','deepseek-v4-pro']:
   a=MockAdapter();out=generate_batch([context('pt-BR'),context('es-MX')],self.root/model,lambda:a,model=model)
   self.assertEqual(out['http_requests'],1);self.assertEqual(len(a.calls),1)
   self.assertIn('COPY_PRESENTATION_ZH_V1',a.calls[0][1][0]['content'])
   for r in out['items']:
    self.assertEqual(copy_warnings(r['response']),[]);self.assertTrue(all(t['text_zh'] for t in r['response']['titles']))
   again=generate_batch([context('pt-BR'),context('es-MX')],self.root/model,lambda:a,model=model)
   self.assertEqual(again['http_requests'],0)
 def test_generic_en_header_reuses_chinese(self):
  from autocut.workbench import table_scripts
  rows=table_scripts({'filename':'en.xlsx','rows':[{'编号':'E1','EN':'A practical buckle','中文':'实用的带扣'}]},'Brazil')
  self.assertEqual(rows[0]['text'],'A practical buckle');self.assertEqual(rows[0]['language'],'en');self.assertEqual(rows[0]['text_zh'],'实用的带扣')
 def test_long_script_missing_zh_does_not_invalidate_foreign(self):
  for lang in ['pt-BR','es-MX']:
   r=response(lang);r['localized_script']['text']*=15;r['localized_script']['text_zh']=ZH*15
   self.assertEqual(validation(r,context(lang))['localized_script']['text_zh'],ZH*15)
   missing=validation(response(lang,False),context(lang));self.assertEqual(copy_warnings(missing),['ZH_TRANSLATION_MISSING'])
   missing=response(lang);missing['localized_script']['text_zh']=None
   self.assertEqual(validation(missing,context(lang))['localized_script']['text_zh'],'')
 def test_xlsx_reuse_batch_visible_and_cross_job_cache(self):
  from openpyxl import Workbook
  p=self.root/'copy.xlsx';w=Workbook();w.active.append(['编号','中文文案','pt-BR','历史表现']);w.active.append(['W4','这是中文原稿',BR,'2500']);w.active.append(['W7','',MX,'500']);w.active.append(['W8','',BR+' Amiga.','100']);w.save(p)
  self.c.post('/native/grant',json={'path':str(p)});rows=self.c.post('/references/inspect',json={'path':str(p)}).json()['scripts']['Brazil']
  self.assertEqual(rows[0]['text'],BR);self.assertEqual(rows[0]['text_zh'],'这是中文原稿')
  payload=dict(scope='table',path=str(p),market='Brazil',ids=[rows[0]['id']]);out=self.c.post('/copy/translate',json=payload).json();self.assertEqual(out['http_requests'],0)
  payload['ids']=[r['id'] for r in rows[1:]];out=self.c.post('/copy/translate',json=payload).json();self.assertEqual(out['http_requests'],1);self.assertTrue(all(r['text_zh'] for r in out['rows']))
  self.assertEqual(self.c.post('/copy/translate',json=payload).json()['http_requests'],0)
  self.c.post('/native/grant',json={'path':str(self.root)})
  j=self.c.post('/jobs',json=dict(request_id='table-snapshot-zh',input_path=str(self.root),product_name='腰带扣',script_mode='TABLE_SELECTED',script_reference_file=str(p),script_reference_id=rows[1]['id'])).json()
  self.assertTrue(j['script_snapshot']['text_zh']);p.unlink();self.assertTrue(Store(self.store.home).get(j['id'])['script_snapshot']['text_zh'])
 def test_legacy_backfill_snapshots_and_manual_gate(self):
  j=self.job();id=j['id'];r=response(zh=False);candidates=copy_candidates({'g':{'response':r}},'g');self.store.item(id,1,copy=r,script_candidates=candidates,title_candidates=r['titles'],selected_title_id=1,title=r['titles'][0]['text']);self.store.update(id,state='WAITING_SCRIPT_APPROVAL')
  raw=copy.deepcopy(self.store.get(id)['items'][0]);self.store.done(id,'COPY_DONE',1,'unchanged',r)
  self.assertEqual(self.c.get('/jobs/'+id).status_code,200);self.assertEqual(len(self.adapter.calls),0)
  self.assertEqual(self.c.post('/jobs/'+id+'/approve-scripts',json={'action_tokens':{x['rank']:x['action_token'] for x in self.c.get('/jobs/'+id).json()['items']}}).json()['error'],'ZH_TRANSLATION_REQUIRED')
  translated=self.c.post('/jobs/'+id+'/translations',json={'kind':'script'}).json();self.assertEqual(translated['http_requests'],1)
  after=self.store.get(id)['items'][0];self.assertEqual(after['script_candidates'],raw['script_candidates']);self.assertEqual(after['copy'],raw['copy'])
  self.assertEqual(self.c.post('/jobs/'+id+'/approve-scripts',json={'action_tokens':{x['rank']:x['action_token'] for x in self.c.get('/jobs/'+id).json()['items']}}).status_code,200)
  self.assertEqual(self.store.get(id)['script_selections']['01']['candidate_sha'],digest(candidates))
  self.store.update(id,state='WAITING_TITLE_APPROVAL');self.assertEqual(self.c.post('/jobs/'+id+'/approve-titles',json={'action_tokens':{x['rank']:x['action_token'] for x in self.c.get('/jobs/'+id).json()['items']}}).status_code,400)
  self.assertEqual(self.c.post('/jobs/'+id+'/approve-titles',json={'allow_original':'yes','action_tokens':{x['rank']:x['action_token'] for x in self.c.get('/jobs/'+id).json()['items']}}).status_code,400)
  self.assertEqual(self.c.post('/jobs/'+id+'/approve-titles',json={'allow_original':True,'action_tokens':{x['rank']:x['action_token'] for x in self.c.get('/jobs/'+id).json()['items']}}).status_code,200);self.assertTrue(self.store.get(id)['title_zh_bypass'])
  self.c.post('/jobs/'+id+'/translations',json={'kind':'title'})
  reopened=Store(self.store.home);view=presented_job(reopened.get(id),reopened.home,'deepseek-flash');self.assertTrue(view['items'][0]['title_text_zh']);self.assertTrue(view['items'][0]['script_text_zh'])
  self.assertEqual(reopened.checkpoint(id,'COPY_DONE',1,'unchanged'),r)
  self.assertEqual(len(self.adapter.calls),2)
 def test_history_and_translation_failure_retry_dedup(self):
  j=self.job();self.store.item(j['id'],1,copy=response(zh=False));rows=self.c.get('/scripts',params={'product':'腰带扣','market':'Brazil'}).json();self.assertFalse(rows[0]['text_zh'])
  payload=dict(scope='existing',product='腰带扣',market='Brazil',ids=[rows[0]['id']]);self.app.state.translations.factory=lambda:(_ for _ in ()).throw(ValueError('NO_KEY'))
  self.assertEqual(self.c.post('/copy/translate',json=payload).json()['warning'],'ZH_TRANSLATION_MISSING')
  self.app.state.translations.factory=lambda:self.adapter
  self.assertTrue(self.c.post('/copy/translate',json=payload).json()['rows'][0]['text_zh'])
  self.assertTrue(self.c.get('/scripts',params={'product':'腰带扣','market':'Brazil'}).json()[0]['text_zh']);self.assertEqual(len(self.adapter.calls),1)
 def test_automatic_missing_chinese_reaches_tts_without_extra_translation(self):
  j=self.job();id=j['id'];m=Manager(self.store,None,None);m.stops[id]=threading.Event()
  source=self.root/'mock.mp4';source.write_bytes(b'mock')
  media=dict(rank=1,path=str(source),filename='mock.mp4',sha256='mock',duration=8.,width=1080,height=1920,status='VALID')
  analysis=dict(voice_quality='CLEAR',clean_script='Original',source_language='en',selling_points=['compact'],allowed_claims=['compact'],promotion_context='',context='Product demonstration',group_key='test',speaker='female',overlays_need_review=False)
  decision=dict(copy_approved=True,issues=[],selected_title_id=1,title_y=.17,subtitle_y=.78,cta_enabled=True,cta_side='left',override_reason='NONE')
  def judge(*args):return copy.deepcopy(analysis if args[5]=='analyze' else decision)
  def copies(contexts,*args):return {'items':[dict(script_group_id=c['script_group_id'],response=response(zh=False)) for c in contexts]}
  future=Future();future.set_result({'text':'Original'});harness=MagicMock();harness.__enter__.return_value=harness
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.Harness',return_value=harness),patch('autocut.pipeline.scan',return_value=[media]),patch('autocut.pipeline.asr_job',return_value=future),patch('autocut.pipeline.source_sheet',return_value='sheet.jpg'),patch.object(m,'judge',side_effect=judge),patch.object(m,'copy_batch',side_effect=copies) as batch,patch.object(m,'catalog',side_effect=ValueError('TEST_STOP_AT_TTS')) as catalog,patch('autocut.copy_presentation.TranslationService.translate') as translation:
   m.run(id);catalog.assert_called_once();batch.assert_called_once();translation.assert_not_called();j=self.store.get(id);self.assertEqual(j['items'][0]['presentation_warnings'],['ZH_TRANSLATION_MISSING']);self.assertEqual(j['issues'],['TEST_STOP_AT_TTS'])
  m.queue.shutdown()
 def test_generic_language_cache_and_media_boundary(self):
  cache=TranslationCache(self.store.home);cache.put('Hello','en','model','你好');self.assertEqual(cache.get('Hello','en','model'),'你好');self.assertEqual(cache.get('Hello','en','other'),'')
  r=response();sentinel='绝不能出现在视频的中文哨兵';r['localized_script']['text_zh']=sentinel
  for t in r['titles']:t['text_zh']=sentinel
  clean=production_copy(r);self.assertNotIn(sentinel,json.dumps(clean,ensure_ascii=False))
  catalog=dict(models=[dict(id='eleven_flash_v2_5',max_chars=5000,languages=[{'language_id':'pt'}],supported={k:True for k in ['speed','stability','similarity_boost']})],voices=[dict(id='fixture',name='Voice')])
  payload,_=tts_payload(r['localized_script']['text'],'fixture','eleven_flash_v2_5',catalog,'pt-BR');self.assertNotIn(sentinel,json.dumps(payload,ensure_ascii=False))
  alignment=self.root/'alignment.json';text=BR;write(alignment,{'alignment':{'characters':list(text),'character_start_times_seconds':[i*.03 for i in range(len(text))],'character_end_times_seconds':[(i+1)*.03 for i in range(len(text))]}})
  source=dict(rank=1,path=str(self.root/'source.mp4'),duration=8.,sha256='fixture',width=1080,height=1920);audio=dict(duration=7.,path=str(self.root/'voice.mp3'),alignment_path=str(alignment),sha256='fixture')
  decision=dict(title_y=.17,subtitle_y=.78,cta_side='left',cta_enabled=False,override_reason='NONE');work=self.root/'work';work.mkdir()
  plan=make_plan(source,audio,r,decision,r['titles'][0]['text'],work,self.root/'out.mp4',{})
  self.assertNotIn(sentinel,json.dumps(plan,ensure_ascii=False));self.assertNotIn('text_zh',json.dumps(plan));self.assertTrue(plan['visual']['subtitle']['captions']);self.assertEqual(plan['visual']['title']['text'],r['titles'][0]['text'])
if __name__=='__main__':unittest.main()
