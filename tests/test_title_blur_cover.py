import copy, json, tempfile, threading, unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from PIL import Image
from autocut import context
from autocut.title_blur_cover import prepare, bind, validate, raster, apply_layers, VERSION
from app.s3.visual_preset import load_preset
from app.s3.headless_runtime import sha
from app.s3.overlay_policy import lock_plan, render_gate, plan_payload

class TitleCoverTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
  evidence=self.folder/'evidence.png';Image.new('RGB',(100,100),'white').save(evidence)
  self.p=dict(source=dict(path=str(evidence),sha256=sha(evidence),duration=20,source_end=20),source_ranges=[[0,20]],video_speed=1.25,target_duration=16,title='Cinto cravando na pele? Tem solução',final_title_language='pt-BR',target_language='pt-BR',visual=dict(canvas=[1080,1920],title=dict(load_preset()['title'],start=0,end=5)),overlay_analysis={'status':'COMPLETE'},overlays=[])
  self.r=dict(kind='BLURRED_TITLE',background='WHITE_CARD',bbox=[.18,.115,.79,.157],original_card_bbox=[.18,.115,.79,.157],source_interval=[0,20],persistent_source_card=False,safe_to_cover=True,confidence=.99,source_sha256=sha(evidence),evidence=[dict(path=str(evidence),sha256=sha(evidence))])
 def test_01_normal_title_untouched(self):
  e={'text_layers':[{'kind':'title','start':0,'end':5}]};old=copy.deepcopy(e);apply_layers(self.p,e,self.folder);self.assertEqual(e,old)
  self.assertNotIn('title_blur_white_cover',plan_payload(self.p))
 def test_02_clear_or_colored_cards_not_whitened(self):
  self.assertIsNone(prepare(self.p,dict(self.r,kind='INTACT_TITLE')))
  self.assertIsNone(prepare(self.p,dict(self.r,background='COLORED_CARD')))
 def test_03_exact_opaque_region_compact_dark_original_font(self):
  bind(self.p,self.r);m=raster(self.p,self.folder)
  with Image.open(m['layer']) as im:
   self.assertEqual(list(im.getbbox()),m['bbox']);l,t,r,b=m['bbox'];self.assertEqual(im.getpixel((l,t)),(255,255,255,255));self.assertEqual(im.getpixel((l-1,t)),(0,0,0,0))
   self.assertIn((23,23,23,255),list(im.getdata()))
  self.assertLessEqual(m['font_size'],self.p['visual']['title']['font_size']);self.assertEqual(m['font_loaded'],'Mathilde Black Italic')
 def test_04_hook_cover_and_text_same_window(self):
  bind(self.p,self.r);e={'text_layers':[{'kind':'title','start':0,'end':5},{'kind':'subtitle','start':0,'end':8}]};apply_layers(self.p,e,self.folder)
  rows=e['text_layers'];self.assertEqual([(x['start'],x['end']) for x in rows if x['kind']==VERSION],[(0,5)]);self.assertFalse(any(x['kind']=='title' for x in rows))
 def test_05_persistent_card_never_empty(self):
  bind(self.p,dict(self.r,persistent_source_card=True));self.assertEqual(self.p['title_blur_white_cover']['intervals'],[[0,16]])
  e={'text_layers':[]};apply_layers(self.p,e,self.folder);self.assertTrue(all(x['text']==self.p['title'] for x in e['text_layers']))
 def test_06_time_mapping_after_cuts(self):
  self.p['source_ranges']=[[0,4],[10,20]];self.p['target_duration']=11.2
  c=prepare(self.p,dict(self.r,source_interval=[11,15],persistent_source_card=True));self.assertEqual(c['intervals'],[[4,7.2]])
 def test_07_no_large_bar_or_card_expansion(self):
  with self.assertRaises(ValueError):prepare(self.p,dict(self.r,bbox=[0,.1,1,.18]))
  with self.assertRaises(ValueError):prepare(self.p,dict(self.r,bbox=[.17,.115,.79,.157]))
 def test_08_background_subject_overlap_abstains(self):
  self.p['subject_protection']={'protected_regions':[{'bbox':[.2,.1,.4,.3],'start':0,'end':20,'kind':'face'}]}
  self.assertIsNone(prepare(self.p,dict(self.r,background='BACKGROUND')))
 def test_09_evidence_changed_rejected(self):
  bind(self.p,self.r);Path(self.r['evidence'][0]['path']).write_bytes(b'changed')
  with self.assertRaises(ValueError):validate(self.p)
 def test_10_language_guard_and_locked_geometry(self):
  bind(self.p,self.r);lock_plan(self.p);self.assertEqual(render_gate(self.p),[])
  self.p['title_blur_white_cover']['bbox']=[.2,.1,.7,.15]
  self.assertIn('OVERLAY_PLAN_CHANGED_AFTER_PREVIEW',render_gate(self.p))
  with self.assertRaises(ValueError):validate(self.p)
 def test_11_unverified_or_normal_title_disabled(self):
  self.assertIsNone(prepare(self.p,dict(self.r,confidence=.5)))
  self.p['title_enabled']=False;self.assertIsNone(prepare(self.p,self.r))
 def test_12_late_card_keeps_initial_hook(self):
  bind(self.p,dict(self.r,source_interval=[10,20],persistent_source_card=True));e={'text_layers':[{'kind':'title','start':0,'end':5}]};apply_layers(self.p,e,self.folder)
  self.assertTrue(any(x['kind']=='title' and x['end']==5 for x in e['text_layers']))
 def test_13_bad_target_title_rejected(self):
  self.p['title']='This belt is very easy to use';bind(self.p,self.r)
  with self.assertRaises(ValueError):validate(self.p)
 def test_14_resume_has_no_paid_upstream_calls(self):
  from autocut.title_blur_cover import request_repair, resume_repair
  store=MagicMock();store.home=self.folder;store.lock=threading.RLock()
  manager=MagicMock();manager.store=store;manager.lock=threading.RLock();manager.item_pending=set()
  item=dict(rank='04',status='PASS',script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,render_revision=2)
  peer=dict(rank='05',status='FAILED',issues=['DURATION_FIT_FAILED']);job=dict(id='job',state='PARTIAL_COMPLETED',input_path=str(self.folder/'source'),items=[item,peer])
  store.get.side_effect=lambda _:copy.deepcopy(job)
  store.item.side_effect=lambda id,rank,**fields:item.update(fields)
  store.update.side_effect=lambda id,**fields:job.update(fields)
  plan=copy.deepcopy(self.p);plan.update(audio={'fingerprint':'paid-audio','path':'cached.mp3'},final_script='unchanged')
  path=self.folder/'jobs/job/work/plans/04.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(plan))
  request_repair(manager,'job','04',self.r);request_repair(manager,'job','04',self.r)
  manager.enqueue_item.assert_called_once_with('job','04',VERSION)
  with patch('app.s3.headless_runtime.Harness'),patch('autocut.item_pipeline.tts_job') as tts:
   resume_repair(manager,'job','04');tts.assert_not_called()
  after=json.loads(path.read_text());self.assertEqual(after['audio'],plan['audio']);self.assertEqual(after['final_script'],plan['final_script']);self.assertEqual(after['visual'],plan['visual'])
  self.assertEqual(peer,dict(rank='05',status='FAILED',issues=['DURATION_FIT_FAILED']));self.assertTrue(after['output'].endswith('_r3.mp4'));manager.finish_plans.assert_called_once()

if __name__=='__main__':unittest.main()
