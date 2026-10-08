"""Onsite regression: real source analysis must retain subjects and call provenance."""
import copy,json,subprocess,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from autocut.drivers import Analysis,AIDriver,CodexDriver,DriverError,PlanDecision
from autocut.pipeline import Manager,copy_source_language
from autocut.storage import Store
from app.s3.headless_runtime import fingerprint

BASE=dict(source_language='en',clean_script='Keep the original source words.',voice_quality='USABLE',speaker='unknown',context='Hands attach side waist straps.',selling_points=['Side waist adjustment'],allowed_claims=['Side waist adjustment'],promotion_context='',group_key='original-facts',existing_arrow=False,overlays_need_review=False,overlay_notes='',only_source_title_overlay=False)
VIS=dict(people_present=True,product_description='Black H-marked waist strap on jeans',main_actions=['Hook and tighten the strap'],protected_regions=[dict(kind='person',bbox=[.1,.0,.9,1.],start=0,end=10,description='Visible torso'),dict(kind='product',bbox=[.25,.4,.75,.6],start=1,end=9,description='Strap at the side waist')])

class VisionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.img=self.root/'frame.jpg';self.img.write_bytes(b'fixture-image')
 def driver(self,result=None):
  d=CodexDriver(self.root);d._request=MagicMock(return_value=Analysis(**(result or dict(BASE,**VIS))));return d
 def test_01_presence_and_regions_retained(self):
  d=self.driver();a=d.analyze({'source':{'duration':10}},[self.img]);self.assertTrue(a.people_present);self.assertEqual(a.protected_regions[1].kind,'product');self.assertIn('partial torsos',d._request.call_args.args[0]);self.assertNotIn('Do not identify people.',d._request.call_args.args[0])
 def test_02_missing_vision_does_not_silently_analyze(self):
  d=self.driver()
  with self.assertRaisesRegex(DriverError,'AI_VISION_INPUT_MISSING'):d.analyze({'source':{'duration':10}})
  d._request.assert_not_called()
 def test_03_missing_product_or_person_evidence_rejected(self):
  for regions in [[],[VIS['protected_regions'][1]],[VIS['protected_regions'][0]]]:
   with self.subTest(regions=regions),self.assertRaisesRegex(DriverError,'AI_VISUAL_EVIDENCE_INCOMPLETE'):
    self.driver(dict(BASE,**dict(VIS,protected_regions=regions))).analyze({'source':{'duration':10}},[self.img])
 def test_04_invalid_coordinates_and_times_rejected(self):
  for fields in [dict(bbox=[.9,.2,.1,.3]),dict(bbox=[0,0,1.1,1]),dict(end=20)]:
   v=copy.deepcopy(VIS);v['protected_regions'][1].update(fields)
   with self.subTest(fields=fields),self.assertRaises(DriverError):self.driver(dict(BASE,**v)).analyze({'source':{'duration':10}},[self.img])
 def test_05_cli_actual_model_and_image_provenance(self):
  def runner(args,**kw):
   if args[1:]==['login','status']:return subprocess.CompletedProcess(args,0,'Logged in','')
   self.assertEqual(args[args.index('--image')+1],str(self.img.resolve()));Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(dict(BASE,**VIS)))
   return subprocess.CompletedProcess(args,0,'','model: real-model-from-cli\n')
  d=CodexDriver(self.root,runner=runner);d.binary='/bin/echo';d.analyze({'source':{'duration':10}},[self.img]);e=d.request_evidence();self.assertEqual(e['model'],'real-model-from-cli');self.assertTrue(e['vision_enabled']);self.assertEqual(e['image_count'],1);self.assertEqual(len(e['image_sha256'][0]),64)
 def test_06_thread_local_provenance(self):
  d=CodexDriver(self.root);barrier=threading.Barrier(2);seen={}
  def run(name):d.record_request(name,[self.img]);barrier.wait();seen[name]=d.request_evidence()['model']
  threads=[threading.Thread(target=run,args=(x,)) for x in ['one','two']]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual(seen,{'one':'one','two':'two'})
 def test_07_legacy_enriched_once_without_rewriting_paid_copy_basis(self):
  s=Store(self.root/'db');j,_=s.create(dict(request_id='vision-onsite-regression',product_name='belt'));id=j['id'];inputs={'source':{'duration':10}}
  s.done(id,'VIDEO_ANALYSIS',1,fingerprint(inputs),BASE);s.done(id,'COPY_DONE',1,'paid',{'final':'unchanged'})
  d=self.driver(dict(BASE,**VIS,group_key='new-wording',clean_script='Do not replace source'))
  d.record_request('model-evidence',[self.img]);router=SimpleNamespace(resolve=lambda mode:d)
  m=Manager(s,MagicMock(),router);self.addCleanup(m.queue.shutdown);m.stops[id]=threading.Event();h=MagicMock()
  a=m.judge(id,h,'VIDEO_ANALYSIS',1,inputs,'analyze',[self.img]);self.assertEqual(a['group_key'],BASE['group_key']);self.assertEqual(a['clean_script'],BASE['clean_script']);self.assertTrue(a['people_present']);self.assertEqual(a['driver_provenance']['model'],'model-evidence');self.assertFalse(a['driver_provenance']['fallback_used'])
  self.assertEqual(m.judge(id,h,'VIDEO_ANALYSIS',1,inputs,'analyze',[self.img]),a);self.assertEqual(d._request.call_count,1);self.assertEqual(s.checkpoint(id,'COPY_DONE',1,'paid'),{'final':'unchanged'})
 def test_08_edit_plan_receives_subject_evidence(self):
  d=self.driver();d.build_edit_plan({'analysis':dict(BASE,**VIS)},[self.img]);task,context,images,schema=d._request.call_args.args;self.assertIn('protected_regions',task);self.assertTrue(context['analysis']['people_present']);self.assertIs(schema,PlanDecision);self.assertIn('subject_avoidance_reason',schema.model_json_schema()['required'])
 def test_09_real_mixed_language_description_fits_provider_contract(self):
  value='English mixed with an unidentified language'
  self.assertGreater(len(value),40);self.assertEqual(copy_source_language(value),'mixed');self.assertEqual(copy_source_language('English'),'en')
 def test_10_retry_only_pre_request_failure_and_preserve_paid_copies(self):
  work=self.root/'work';p=work/'scripts/copy_results.json';p.parent.mkdir(parents=True)
  old={'good':{'response':{'script':'unchanged'}},'bad':{'error':'INVALID_DEEPSEEK_CONTEXT'},'uncertain':{'error':'PRIOR_ATTEMPT_REVIEW_REQUIRED'}};p.write_text(json.dumps(old))
  m=Manager(MagicMock(),MagicMock(),MagicMock());self.addCleanup(m.queue.shutdown);m.copy_batch=MagicMock(return_value={'items':[{'script_group_id':'bad','response':{'script':'localized'}}]})
  contexts=[{'script_group_id':k} for k in old];r=m.prepare_copies(contexts,work,'model');self.assertEqual(m.copy_batch.call_args.args[0],[{'script_group_id':'bad'}]);self.assertEqual(r['good'],old['good']);self.assertEqual(r['uncertain'],old['uncertain']);m.prepare_copies(contexts,work,'model');self.assertEqual(m.copy_batch.call_count,1)

if __name__=='__main__':unittest.main()
