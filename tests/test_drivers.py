import json,subprocess,tempfile,unittest
from pathlib import Path
import httpx
from autocut.drivers import *
from autocut.storage import Store

DEC=dict(selected_title_id=2,selection_reason='Matches visible product',title_y=.17,subtitle_y=.78,cta_side='left',cta_enabled=True,override_reason='NONE',copy_approved=True,issues=[])
class Secrets:
 def __init__(self,value=''):self.value=value
 def get(self,name):return self.value
class DriverTests(unittest.TestCase):
 def test_health_and_auto(self):
  with tempfile.TemporaryDirectory() as d:
   c=CodexDriver(d);c.binary='/bin/echo'
   c.runner=lambda *a,**k:subprocess.CompletedProcess(a,0,'Logged in using ChatGPT','')
   o=OpenAIDriver(Secrets());router=DriverRouter(c,o)
   self.assertIs(router.resolve('auto'),c)
   c.runner=lambda *a,**k:subprocess.CompletedProcess(a,1,'','Not logged in')
   self.assertEqual(c.health().status,'AUTH_REQUIRED')
   with self.assertRaises(DriverError):router.resolve('auto')
   o.secrets=Secrets('test-secret-not-real');self.assertIs(router.resolve('auto'),o)
   with self.assertRaises(DriverError):router.resolve('codex')
   c.runner=lambda *a,**k:subprocess.CompletedProcess(a,1,'','unexpected CLI error')
   self.assertEqual(c.health().status,'UNAVAILABLE')
   self.assertIs(router.resolve('auto'),o)
 def test_equal_schemas_and_no_legacy_api(self):
  def transport(req):
   self.assertEqual(req.url.path,'/v1/responses');data=json.loads(req.content)
   self.assertEqual(data['text']['format']['schema'],PlanDecision.model_json_schema())
   self.assertFalse(data['store'])
   return httpx.Response(200,json={'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(DEC)}]}]})
  o=OpenAIDriver(Secrets('test-secret-not-real'),transport=httpx.MockTransport(transport))
  def runner(args,**kwargs):
   if args[1:]==['login','status']:return subprocess.CompletedProcess(args,0,'Logged in','')
   self.assertIn('read-only',args);self.assertIn('--ephemeral',args)
   Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(DEC))
   return subprocess.CompletedProcess(args,0,'','')
  with tempfile.TemporaryDirectory() as d:
   c=CodexDriver(d,runner=runner);c.binary='/bin/echo'
   self.assertEqual(c.build_edit_plan({'test':True}),o.build_edit_plan({'test':True}))
 def test_driver_switch_keeps_checkpoints_and_upgrade(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(d);j,_=s.create({'request_id':'test-request-id','product_name':'test'})
   for stage in ['ASR_DONE','COPY_DONE','TTS_DONE']:s.done(j['id'],stage,'01','unchanged',{'result':'paid-success'})
   s.set_setting('ai_driver','openai_api');s.save_product({'id':'custom','product_name':'persist'})
   s.update(j['id'],state='RUNNING');s2=Store(d)
   self.assertEqual(s2.get(j['id'])['state'],'INTERRUPTED');self.assertEqual(s2.product('custom')['product_name'],'persist')
   for stage in ['ASR_DONE','COPY_DONE','TTS_DONE']:self.assertEqual(s2.checkpoint(j['id'],stage,'01','unchanged'),{'result':'paid-success'})
   self.assertEqual(s2.create({'request_id':'test-request-id'})[0]['id'],j['id'])
 def test_review_cannot_silently_pass_failed_check(self):
  with tempfile.TemporaryDirectory() as d:
   c=CodexDriver(d)
   with self.assertRaises(DriverError):c.decode(json.dumps(dict(status='PASS',title=False,subtitle=True,cta=True,overlay=True,source_text=True,watermark=True,product_preserved=True,issues=[],reason='bad')),Review)
if __name__=='__main__':unittest.main()
