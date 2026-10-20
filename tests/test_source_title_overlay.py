import copy, tempfile, unittest
from pathlib import Path
from PIL import Image
from autocut import context
from autocut.source_title_overlay import POLICY, bind, snapshot, verify_layers
from autocut.title_blur_cover import prepare, bind as bind_cover, raster, apply_layers
from autocut.title_layout import apply as fit_title
from autocut.server import NewJob
from app.s3.headless_runtime import sha
from app.s3.visual_preset import load_preset, text_layer, validate_visual_plan

class OverlayTaskTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.frame=self.root/'evidence.png';Image.new('RGB',(576,1024),'white').save(self.frame)
  preset=load_preset();visual=dict(visual_preset=preset['id'],preset_snapshot=copy.deepcopy(preset),canvas=[1080,1920],duration=20,
   title=dict(preset['title'],text='Ajuste fácil sem apertar',start=0,end=5),subtitle=dict(preset['subtitle'],captions=[]),
   cta=dict(preset['cta'],start=15,end=20),overrides=[],visual_preset_override=False)
  fit_title(visual,visual['title']['text'],max_lines=2)
  self.p=dict(rank='01',source=dict(path=str(self.frame),sha256=sha(self.frame),duration=20,source_end=20,width=576,height=1024),
   source_ranges=[[0,20]],video_speed=1,target_duration=20,title='Ajuste fácil sem apertar',target_language='pt-BR',final_title_language='pt-BR',
   title_enabled=True,visual=visual,overlay_analysis=dict(status='COMPLETE'),overlays=[],task_overrides={'source_title_overlay_policy':POLICY},subject_protection={'protected_regions':[]})
  self.region=dict(kind='INTACT_TITLE',background='WHITE_CARD',bbox=[.08,.60,.9,.71],original_card_bbox=[.08,.60,.9,.71],source_interval=[0,12],
   persistent_source_card=True,safe_to_cover=True,confidence=.99,source_sha256=sha(self.frame),evidence=[dict(path=str(self.frame),sha256=sha(self.frame))])
 def test_01_middle_white_caption_opt_in_only(self):
  self.assertEqual(prepare(self.p,self.region)['intervals'],[[0,12]])
  old=copy.deepcopy(self.p);old.pop('task_overrides')
  with self.assertRaisesRegex(ValueError,'UNSAFE'):prepare(old,self.region)
 def test_02_no_expansion_and_no_large_plates(self):
  for change in [dict(bbox=[.07,.60,.9,.71]),dict(bbox=[0,.5,1,.8],original_card_bbox=[0,.5,1,.8])]:
   with self.assertRaises(ValueError):prepare(self.p,dict(self.region,**change))
 def test_03_short_hook_never_leaves_source_caption_exposed(self):
  self.region.update(persistent_source_card=False,kind='BLURRED_TITLE');bind_cover(self.p,self.region)
  self.assertEqual(self.p['title_blur_white_cover']['intervals'],[[0,12]])
  bind(self.p,self.root);e={'text_layers':[dict(kind='title',start=0,end=5,bbox=[1,2,3,4])]};apply_layers(self.p,e,self.root)
  self.assertEqual([(r['start'],r['end']) for r in e['text_layers']],[(0,12)])
  self.assertEqual(len(e['text_layers'][0]['lines']),2);self.assertGreaterEqual(e['text_layers'][0]['font_size'],48)
 def test_04_sticker_survives_and_title_moves(self):
  self.p['subject_protection']['protected_regions']=[dict(kind='sticker',bbox=[0,0,1,.31],start=0,end=5)]
  before=copy.deepcopy(self.p['source']);bind(self.p,self.root)
  self.assertGreater(self.p['visual']['title']['anchor_y'],.31)
  self.assertEqual(self.p['source'],before);self.assertEqual(self.p['visual']['title']['end'],5)
  self.assertEqual(validate_visual_plan(self.p['visual']),[])
  layer=text_layer(self.p['title'],self.p['visual']['title'],[1080,1920],self.root/'real.png')
  e={'text_layers':[dict(layer,kind='title',start=0,end=5)]};apply_layers(self.p,e,self.root)
  self.assertEqual(e['source_title_overlay']['sticker_check'],'PASS')
 def test_05_no_safe_position_keeps_review_gate(self):
  self.p['subject_protection']['protected_regions']=[dict(kind='sticker',bbox=[0,0,1,1],start=0,end=5)]
  with self.assertRaisesRegex(ValueError,'TITLE_STICKER_LAYOUT_REQUIRED'):bind(self.p,self.root)
 def test_06_card_cannot_cover_a_sticker(self):
  self.p['subject_protection']['protected_regions']=[dict(kind='sticker',bbox=[.5,.6,.95,.9],start=1,end=10)]
  bind_cover(self.p,self.region)
  with self.assertRaisesRegex(ValueError,'TITLE_STICKER_LAYOUT_REQUIRED'):bind(self.p,self.root)
 def test_07_disabled_policy_preserves_history(self):
  self.p.pop('task_overrides');before=copy.deepcopy(self.p);bind(self.p,self.root);self.assertEqual(self.p,before)
  e={'text_layers':[]};verify_layers(self.p,e);self.assertEqual(e,{'text_layers':[]})
 def test_08_snapshot_is_explicit_and_validated(self):
  base={'resolved_overrides_json':{'values':{}},'instruction_summary':[]}
  self.assertEqual(snapshot(base,{}),base)
  result=snapshot(base,{'source_title_overlay_policy':POLICY});self.assertEqual(result['resolved_overrides_json']['values']['source_title_overlay_policy'],POLICY)
  self.assertEqual(base['resolved_overrides_json']['values'],{})
  self.assertEqual(NewJob(request_id='belt-current-task',input_path='/source',source_title_overlay_policy=POLICY).source_title_overlay_policy,POLICY)
  with self.assertRaises(ValueError):NewJob(request_id='belt-current-task',input_path='/source',source_title_overlay_policy='UNBOUNDED')
 def test_09_real_layer_collision_is_blocked(self):
  self.p['subject_protection']['protected_regions']=[dict(kind='sticker',bbox=[0,0,1,.31],start=0,end=5)]
  bind(self.p,self.root)
  with self.assertRaisesRegex(ValueError,'TITLE_STICKER_LAYOUT_REQUIRED'):
   verify_layers(self.p,{'text_layers':[dict(kind='title',bbox=[1,1,200,200],start=0,end=5)]})
 def test_10_cover_time_maps_source_cuts_and_speed(self):
  self.p.update(source_ranges=[[0,3],[8,18]],video_speed=1.25,target_duration=10.4)
  self.region['source_interval']=[8,12];self.assertEqual(prepare(self.p,self.region)['intervals'],[[2.4,5.6]])
 def test_11_default_preset_is_not_extended(self):
  v=self.p['visual'];default=load_preset()['title'];v['title']['anchor_y']=.4
  v.update(visual_preset_override=True,overrides=[dict(layer='title',override_reason='SOURCE_TEXT_CONFLICT',default_position=[default['anchor_x'],default['anchor_y']],chosen_position=[.5,.4])])
  self.assertIn('TITLE_PRESET_DEVIATION',validate_visual_plan(v))
 def test_12_tight_card_uses_exact_mask_and_ordinary_two_line_title(self):
  self.region.update(bbox=[.3,.6,.6,.65],original_card_bbox=[.3,.6,.6,.65])
  bind_cover(self.p,self.region);bind(self.p,self.root)
  layer=raster(self.p,self.root)
  self.assertTrue(layer['mask_only']);self.assertEqual(layer['bbox'],[324,1152,648,1248])
  e={'text_layers':[dict(kind='title',start=0,end=5,bbox=[1,2,3,4])]};apply_layers(self.p,e,self.root)
  self.assertTrue(any(r['kind']=='title' and r['end']==5 for r in e['text_layers']))
 def test_12b_very_long_card_text_keeps_source_mask(self):
  self.p['title']='Uma palavra muito comprida ' * 25;bind_cover(self.p,self.region)
  self.assertTrue(raster(self.p,self.root)['mask_only'])
 def test_13_white_card_fit_is_checked_before_confirmation(self):
  from autocut.production_preflight import title_check
  job=dict(production_preflight_policy='PRODUCTION_PREFLIGHT_V1',market='Brazil',target_language='pt-BR',resolved_overrides_json=dict(values=self.p['task_overrides']))
  item=dict(rank='01',source_snapshot=self.p['source'],analysis=dict(title_blur_region=self.region))
  good=title_check(job,item,self.p['title']);self.assertEqual(good['status'],'PASS');self.assertEqual(len(good['source_white_card']['lines']),2)
  narrow=dict(self.region,bbox=[.3,.6,.6,.65],original_card_bbox=[.3,.6,.6,.65])
  item['analysis']['title_blur_region']=narrow
  checked=title_check(job,item,'Ajuste fácil sem apertar a cintura da sua calça')
  self.assertEqual(checked['status'],'PASS');self.assertIn('overlay_warning',checked)
 def test_15_model_risk_and_stacked_card_do_not_block_explicit_task(self):
  from autocut.production_preflight import title_check
  self.region.update(safe_to_cover=False,confidence=.82,bbox=[.165,.137,.835,.286],original_card_bbox=[.165,.137,.835,.286])
  job=dict(production_preflight_policy='PRODUCTION_PREFLIGHT_V1',market='Brazil',target_language='pt-BR',resolved_overrides_json=dict(values=self.p['task_overrides']))
  item=dict(rank='01',source_snapshot=self.p['source'],analysis=dict(title_blur_region=self.region))
  self.assertEqual(title_check(job,item,self.p['title'])['status'],'PASS')
  self.assertIsNotNone(bind_cover(self.p,self.region));bind(self.p,self.root)
  old=copy.deepcopy(self.p);old['task_overrides']={}
  self.assertIsNone(prepare(old,self.region))
 def test_16_invalid_ai_region_is_advisory_but_normal_title_fit_remains(self):
  from autocut.production_preflight import title_check
  job=dict(production_preflight_policy='PRODUCTION_PREFLIGHT_V1',market='Brazil',target_language='pt-BR',resolved_overrides_json=dict(values=self.p['task_overrides']))
  item=dict(rank='01',source_snapshot=self.p['source'],analysis=dict(title_blur_region={'kind':'INTACT_TITLE','background':'WHITE_CARD','safe_to_cover':False}))
  checked=title_check(job,item,self.p['title']);self.assertEqual(checked['status'],'PASS');self.assertIn('overlay_warning',checked)
  self.assertEqual(title_check(job,item,'Uma palavra muito comprida ' * 25)['status'],'BLOCKED')
 def test_17_point_observations_map_one_frame_without_hard_failure(self):
  from autocut.source_title_overlay import map_regions
  self.p['subject_protection']['protected_regions']=[dict(kind='hands',bbox=[0,0,1,1],start=1.,end=1.)]
  mapped=map_regions(self.p,{'hands'});self.assertAlmostEqual(mapped[0]['end']-mapped[0]['start'],1/30)
  bind(self.p,self.root)
  self.assertIn('TITLE_SUBJECT_LAYOUT_REQUIRES_VISUAL_QA',self.p['title_cover_warnings'])
 def test_18_coarse_subject_boxes_are_advisory_but_sticker_avoidance_remains(self):
  self.p['subject_protection']['protected_regions']=[dict(kind='hands',bbox=[0,0,1,1],start=0,end=5),dict(kind='sticker',bbox=[0,0,1,.31],start=0,end=5)]
  bind(self.p,self.root)
  self.assertGreater(self.p['visual']['title']['anchor_y'],.31)
  self.assertIn('TITLE_SUBJECT_LAYOUT_REQUIRES_VISUAL_QA',self.p['title_cover_warnings'])
 def test_19_plan_builder_does_not_reintroduce_the_preflight_block(self):
  from autocut.pipeline import make_plan
  source=dict(self.p['source'],rank=1)
  audio=dict(path=str(self.frame),sha256=sha(self.frame),duration=20)
  decision=dict(title_y=.17,subtitle_y=.78,override_reason='NONE',title_blur_region={'kind':'INTACT_TITLE','background':'WHITE_CARD','safe_to_cover':False})
  result=make_plan(source,audio,{'titles':[{'text':self.p['title'],'language':'pt-BR'}]},decision,self.p['title'],self.root,self.root/'unused.mp4',{},task_overrides=dict(self.p['task_overrides'],title_max_lines=2),music_only=True,target='pt-BR')
  self.assertIn('TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED',result['title_cover_warnings'])
  self.assertFalse(result.get('title_blur_white_cover'))
  self.assertEqual(len(result['visual']['title_layout']['lines']),2)
 def test_20_montage_card_maps_only_its_source_and_segment_speed(self):
  self.p['montage_segments']=[dict(sha256='donor',source_start=0,source_end=3,output_start=0,output_end=3,speed=1),dict(sha256=self.p['source']['sha256'],source_start=0,source_end=10,output_start=3,output_end=8,speed=2),dict(sha256='donor',source_start=0,source_end=12,output_start=8,output_end=20,speed=1)]
  self.assertEqual(prepare(self.p,self.region)['intervals'],[[3,8]])
  bind_cover(self.p,self.region);bind(self.p,self.root)
  e={'text_layers':[dict(kind='title',start=0,end=5,bbox=[1,2,3,4])]};apply_layers(self.p,e,self.root)
  self.assertEqual([(r['kind'],r['start'],r['end']) for r in e['text_layers']],[('TITLE_BLUR_WHITE_COVER',3,8),('title',0,3)])
  old=copy.deepcopy(self.p);old['task_overrides']={};self.assertIsNone(prepare(old,self.region))
 def test_21_trimmed_primary_montage_keeps_only_visible_caption_interval(self):
  self.region['source_interval']=[0,4]
  self.p['montage_segments']=[dict(sha256=self.p['source']['sha256'],source_start=1,source_end=20,output_start=0,output_end=19,speed=1)]
  self.assertEqual(prepare(self.p,self.region)['intervals'],[[0,3]])
 def test_14_analysis_receives_explicit_overlay_intent(self):
  from unittest.mock import MagicMock
  from autocut.drivers import CodexDriver, Analysis
  from test_production_vision import BASE, VIS
  driver=CodexDriver(self.root);driver._request=MagicMock(return_value=Analysis(**dict(BASE,**VIS)))
  driver.analyze(dict(source={'duration':10},task_overrides={'source_title_overlay_policy':POLICY}),[self.frame])
  self.assertIn('source_title_overlay_requirements',driver._request.call_args.args[1])
  driver.analyze(dict(source={'duration':10}),[self.frame])
  self.assertNotIn('source_title_overlay_requirements',driver._request.call_args.args[1])

if __name__=='__main__':unittest.main()
