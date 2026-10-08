import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.server import create_app
from autocut.provider_models import MODEL_DEFAULTS,MODEL_OPTIONS,job_models,tts_payload
from app.s2.deepseek import DeepSeekAdapter
from app.s2.providers import Official
from app.s3.deepseek_batch import generate_batch
from app.s3.headless_runtime import fingerprint

CONTEXT=dict(script_group_id='one',dedupe_reviewed=True,source_language='en',source_script='Everything in one small bag',clean_script='Everything in one small bag',video_context='Hands open the small bag',product='Small bag',supported_selling_points=['organization'],allowed_claims=['organization'],blocked_claims=[],promotion_context=None,target_duration_seconds=15,market='Brazil',language='pt-BR',title_count=3)
RESULT=dict(localized_script=dict(language='pt-BR',text='Olha como fica tudo no seu lugar com essa bolsa para você sair sem complicar',estimated_style='ugc_tiktok',notes=[]),titles=[dict(id=1,text='Sua bolsa virou uma bagunça',angle='pain_hook'),dict(id=2,text='Tudo no seu lugar',angle='product_benefit'),dict(id=3,text='Era isso que eu precisava',angle='emotion')])

class ModelsTests(unittest.TestCase):
 def test_settings_persist_snapshot_and_atomic_validation(self):
  with tempfile.TemporaryDirectory() as d:
   class Manager:
    def enqueue(self,id):pass
   store=Store(d);c=TestClient(create_app(store,None,None,Manager(),'local-test'),base_url='http://127.0.0.1:9000',headers={'Authorization':'Bearer local-test'})
   c.post('/native/grant',json={'path':d})
   spec=dict(request_id='model-request-1',input_path=d,product_profile_id='belt')
   original=c.post('/jobs',json=spec).json();self.assertEqual(original['provider_models'],MODEL_DEFAULTS)
   store.done(original['id'],'TTS_DONE','01','fingerprint',{'path':'existing.mp3'})
   self.assertEqual(c.post('/settings',json={'deepseek_model':'deepseek-v4-pro','elevenlabs_model':'eleven_v4'}).status_code,200)
   fresh=c.post('/jobs',json=dict(spec,request_id='model-request-2')).json()
   self.assertEqual(fresh['provider_models'],{'deepseek':'deepseek-v4-pro','elevenlabs':'eleven_v4'})
   self.assertEqual(c.post('/jobs',json=spec).json()['provider_models'],MODEL_DEFAULTS)
   before=store.settings();self.assertEqual(c.post('/settings',json={'deepseek_model':'deepseek-flash','elevenlabs_model':'arbitrary-model'}).status_code,400);self.assertEqual(before,store.settings())
   self.assertEqual(c.post('/jobs',json=dict(spec,provider_models={'elevenlabs':'bad'})).status_code,422)
   reopened=Store(d);self.assertEqual(reopened.settings(),before);self.assertEqual(reopened.get(original['id'])['provider_models'],MODEL_DEFAULTS);self.assertEqual(reopened.checkpoint(original['id'],'TTS_DONE','01','fingerprint'),{'path':'existing.mp3'})
   self.assertEqual(job_models({}),{'deepseek':'deepseek-chat','elevenlabs':'eleven_flash_v2_5'})

 def test_batch_selected_model_wire_and_cache_isolation(self):
  calls=[]
  def handler(request):
   body=json.loads(request.content);calls.append(body)
   items=json.loads(body['messages'][1]['content'])['items']
   data={'items':[{'id':x['id'],'result':RESULT} for x in items]}
   return httpx.Response(200,json={'model':body['model'],'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}],'usage':{'total_tokens':12}})
  def factory():
   a=object.__new__(DeepSeekAdapter);a._key='fixture-not-a-real-secret';a.client=httpx.Client(transport=httpx.MockTransport(handler));return a
  with tempfile.TemporaryDirectory() as d:
   first=generate_batch([CONTEXT],d,adapter_factory=factory,model='deepseek-flash')
   self.assertEqual(first['http_requests'],1);self.assertEqual(calls[0]['model'],'deepseek-flash');self.assertEqual(calls[0]['thinking'],{'type':'disabled'})
   hit=generate_batch([CONTEXT],d,adapter_factory=factory,model='deepseek-flash');self.assertEqual(hit['cache_hits'],1);self.assertEqual(len(calls),1)
   second=generate_batch([CONTEXT],d,adapter_factory=factory,model='deepseek-v4-pro')
   self.assertEqual(second['http_requests'],1);self.assertEqual(calls[-1]['model'],'deepseek-v4-pro')
   self.assertNotEqual(first['items'][0]['fingerprint'],second['items'][0]['fingerprint'])
   self.assertEqual(second['items'][0]['request_context']['model'],'deepseek-v4-pro')
   for p in Path(d).rglob('*.json'):self.assertNotIn('fixture-not-a-real-secret',p.read_text())

 def test_all_tts_models_reach_timing_endpoint_with_supported_settings(self):
  catalog=dict(models=[dict(id=m['id'],max_chars=5000,languages=[{'language_id':'pt'},{'language_id':'es'}],supported={k:True for k in ['speed','stability','similarity_boost','style','use_speaker_boost']}) for m in MODEL_OPTIONS['elevenlabs']],voices=[dict(id='voice-fixture',name='Voice')])
  original=copy.deepcopy(catalog);fingerprints=set()
  for option in MODEL_OPTIONS['elevenlabs']:
   mid=option['id'];payload,_=tts_payload('Olá você gosta dessa bolsa','voice-fixture',mid,catalog,'pt-BR');fingerprints.add(fingerprint(payload))
   if mid.startswith('eleven_v4'):self.assertEqual(payload['settings'],{'stability':.5,'similarity_boost':.75})
   elif mid=='eleven_v3':self.assertEqual(payload['settings'],{'stability':.5})
   else:self.assertEqual(payload['settings']['speed'],1.)
   def handler(request):
    self.assertTrue(request.url.path.endswith('/with-timestamps'));body=json.loads(request.content);self.assertEqual(body['model_id'],mid);self.assertEqual(body['voice_settings'],payload['settings'])
    return httpx.Response(200,json={'audio_base64':'eA==','alignment':{'characters':['x']}})
   p=Official('elevenlabs','fixture',client=httpx.Client(transport=httpx.MockTransport(handler)))
   try:self.assertEqual(p.tts(payload)['audio'],b'x')
   finally:p.client.close()
  self.assertEqual(len(fingerprints),len(MODEL_OPTIONS['elevenlabs']));self.assertEqual(catalog,original)
  with self.assertRaisesRegex(ValueError,'PROVIDER_MODEL_LANGUAGE_UNSUPPORTED'):tts_payload('test','voice-fixture','eleven_v4',catalog,'en-US')
  with self.assertRaisesRegex(ValueError,'PROVIDER_MODEL_UNAVAILABLE'):tts_payload('test','voice-fixture','unknown',catalog,'pt-BR')

 def test_manager_uses_selected_model_without_changing_shared_cache(self):
  from autocut.pipeline import Manager
  class Keys:
   def get(self,k):return 'fixture-not-a-real-secret'
  with tempfile.TemporaryDirectory() as d,patch('autocut.pipeline.HOME',Path(d)),patch('autocut.pipeline.generate_batch',return_value={'items':[]}) as batch:
   manager=Manager(None,Keys(),None)
   try:
    manager.copy_batch([CONTEXT],Path(d)/'work','deepseek-v4-pro')
    self.assertEqual(batch.call_args.kwargs['model'],'deepseek-v4-pro')
   finally:manager.queue.shutdown()

if __name__=='__main__':unittest.main()
