import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from autocut import context
from autocut.cta_policy import required,ensure_plan,check_plan,retain_source_arrow,verify_or_repair,VERSION
from app.s3.visual_preset import build_visual_plan,validate_visual_plan
from app.s3.headless_runtime import sha
from PIL import Image,ImageFilter,ImageChops


class CTARequiredTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)
  self.asset=context.asset()
  self.p=dict(rank='01',target_duration=12,video_speed=1.2,source=dict(path='source.mp4',sha256='source',duration=14.4,source_end=14.4),source_ranges=[[0,14.4]],
   visual=build_visual_plan(12,'Esse cinto é confortável',[],asset=self.asset),task_overrides={})
  self.p['visual']['cta']['enabled']=False
  self.p['visual']['user_revision']={'omit_added_cta':True}

 def test_unspecified_and_ai_disable_default_on(self):
  self.p['decision']={'cta_enabled':False};self.p['resolved_parameters']={'cta_enabled':{'value':False,'source':'AI_DECISION'}}
  ensure_plan(self.p);self.assertTrue(self.p['visual']['cta']['enabled']);self.assertEqual(self.p['cta_policy']['origin'],'DEFAULT_ON')
  self.assertFalse(self.p['visual']['user_revision']['omit_added_cta']);check_plan(self.p)

 def test_user_explicit_off_is_only_opt_out(self):
  self.p['task_overrides']['cta_enabled']=False;ensure_plan(self.p)
  self.assertFalse(required(self.p));self.assertFalse(self.p['visual']['cta']['enabled']);check_plan(self.p)

 def test_user_off_needs_no_asset(self):
  self.p['task_overrides']['cta_enabled']=False;self.p['visual'].pop('cta')
  with patch('autocut.cta_policy.asset') as asset:ensure_plan(self.p);asset.assert_not_called()

 def test_explicit_on(self):
  self.p['task_overrides']['cta_enabled']=True;ensure_plan(self.p)
  self.assertEqual(self.p['cta_policy']['origin'],'USER_EXPLICIT_ON')

 def test_transport_remix_music_all_markets(self):
  for mode in ['Transport','Remix']:
   for voice in ['TTS','MUSIC_ONLY']:
    for language in ['pt-BR','es-MX','en']:
     p=copy.deepcopy(self.p);p.update(mode=mode,voiceover_mode=voice,target_language=language);ensure_plan(p)
     self.assertTrue(p['visual']['cta']['enabled'])

 def test_missing_plan_asset_and_window_autocompleted(self):
  self.p['visual'].pop('cta');ensure_plan(self.p);check_plan(self.p)
  self.assertTrue(Path(self.p['visual']['cta']['asset']['file_path']).is_file())
  self.assertEqual(validate_visual_plan(self.p['visual']),[])

 def test_missing_asset_never_enters_renderer(self):
  ensure_plan(self.p);self.p['visual']['cta']['asset']['file_path']=str(self.folder/'missing')
  with self.assertRaisesRegex(ValueError,'CTA_ASSET_UNAVAILABLE'):check_plan(self.p)

 def test_speed_and_short_clip_final_window(self):
  for speed,duration in [(1,12),(1.35,8.2),(1.5,3)]:
   p=copy.deepcopy(self.p);p.update(video_speed=speed,target_duration=duration);ensure_plan(p)
   self.assertEqual((p['visual']['cta']['start'],p['visual']['cta']['end']),(max(0,duration-5),duration))

 def test_batch_ten_independent_items(self):
  rows=[]
  for n in range(10):
   p=copy.deepcopy(self.p);p.update(rank=str(n),final_script=f'original {n}',title=f'title {n}');ensure_plan(p);rows.append(p)
  self.assertEqual(sum(required(p) and p['visual']['cta']['enabled'] for p in rows),10)
  self.assertEqual(len({p['final_script'] for p in rows}),10)

 def test_source_arrow_is_not_enough_without_kept_pixels(self):
  self.p['source_arrow_hint']=True;ensure_plan(self.p)
  with patch('autocut.cta_policy.subprocess.run') as ff,patch('autocut.cta_policy.verify',return_value={'status':'FAIL'}) as pixels:
   self.assertFalse(retain_source_arrow(self.p,self.folder));pixels.assert_called_once()
   args=ff.call_args.args[0];self.assertAlmostEqual(float(args[args.index('-ss')+1]),8.4)
  self.assertTrue(self.p['visual']['cta']['enabled'])

 def test_source_arrow_verified_in_final_window_retained(self):
  self.p['source_arrow_hint']=True;ensure_plan(self.p)
  with patch('autocut.cta_policy.subprocess.run'),patch('autocut.cta_policy.verify',return_value={'status':'PASS','output_sha256':'pixels'}):
   self.assertTrue(retain_source_arrow(self.p,self.folder))
  self.assertFalse(self.p['visual']['cta']['enabled']);self.assertTrue(required(self.p));check_plan(self.p)
  self.p['video_speed']=1.5
  with self.assertRaisesRegex(ValueError,'CTA_PLAN_REQUIRED'):check_plan(self.p)

 def test_ensure_clears_stale_source_arrow_proof(self):
  self.p['cta_retained_evidence']={'status':'PASS'};ensure_plan(self.p)
  self.assertNotIn('cta_retained_evidence',self.p);self.assertTrue(self.p['visual']['cta']['enabled'])

 def test_existing_final_arrow_not_added_twice(self):
  p=self.folder/'output.mp4';p.write_bytes(b'existing');ensure_plan(self.p)
  with patch('autocut.cta_policy.verify',return_value={'status':'PASS'}),patch('autocut.cta_policy.overlay_only') as overlay:
   result=verify_or_repair(self.p,p,self.folder,self.folder/'new.mp4');self.assertFalse(result['repaired']);overlay.assert_not_called()

 def test_missing_final_arrow_repairs_once_no_tts(self):
  ensure_plan(self.p)
  with patch('autocut.cta_policy.verify',side_effect=[{'status':'FAIL'},{'status':'PASS'}]),patch('autocut.cta_policy.overlay_only',return_value={}) as overlay,patch('autocut.item_pipeline.tts_job') as tts:
   r=verify_or_repair(self.p,'old.mp4',self.folder,'new.mp4');self.assertTrue(r['repaired']);overlay.assert_called_once();tts.assert_not_called()

 def test_crash_after_overlay_reuses_verified_output(self):
  ensure_plan(self.p);base=self.folder/'base.mp4';base.write_bytes(b'old');out=self.folder/'repaired.mp4';out.write_bytes(b'repaired')
  with patch('autocut.cta_policy.verify',side_effect=[{'status':'FAIL'},{'status':'PASS'}]),patch('autocut.cta_policy.overlay_only') as overlay:
   r=verify_or_repair(self.p,base,self.folder,out);self.assertTrue(r['repaired']);overlay.assert_not_called()

 def test_failed_repair_is_hard_failure_not_pass(self):
  ensure_plan(self.p)
  with patch('autocut.cta_policy.verify',return_value={'status':'FAIL'}),patch('autocut.cta_policy.overlay_only') as overlay:
   with self.assertRaisesRegex(ValueError,'CTA_RENDER_FAILED'):verify_or_repair(self.p,'old',self.folder,'new')
   overlay.assert_called_once()
   with self.assertRaisesRegex(ValueError,'CTA_RENDER_FAILED'):verify_or_repair(self.p,'old',self.folder,'new',False)
   self.assertEqual(overlay.call_count,1)

 def test_final_pass_gate_requires_output_bound_pixel_proof(self):
  from autocut.ownership import final_ready
  p=self.folder/'out.mp4';p.write_bytes(b'mp4');plan=copy.deepcopy(self.p);ensure_plan(plan);plan['output']=str(p)
  cp={'FINAL_RENDERED':{'input_sha':'same','data':{}},'QA_DONE':{'input_sha':'same','data':{'status':'PASS'}}}
  with patch('autocut.ownership.read',return_value=plan),patch('autocut.ownership.language_issues',return_value=[]):
   self.assertFalse(final_ready(MagicMock(),{'id':'job'},{'rank':'01','output':str(p),'plan':'p'},cp,False))
   cp['QA_DONE']['data']['cta']={'status':'PASS','output_sha256':'wrong-file'}
   self.assertFalse(final_ready(MagicMock(),{'id':'job'},{'rank':'01','output':str(p),'plan':'p'},cp,False))

 def test_same_asset_style_only_vertical_animation(self):
  original=copy.deepcopy(self.p['visual']['cta']);ensure_plan(self.p);c=self.p['visual']['cta']
  for k in ['asset','width_ratio','animation','horizontal_motion','bounce_period_seconds','bounce_amplitude_ratio']:
   self.assertEqual(c[k],original[k])
  self.assertEqual(c['width_ratio'],.16);self.assertEqual(c['animation'],'vertical_bounce')

 def test_pixel_match_does_not_confuse_skin_or_red_shirt_with_arrow(self):
  from autocut.cta_policy import sprite,red_mask,white_mask,artwork_score
  ensure_plan(self.p);s=sprite(self.p,self.folder)
  with Image.open(s['path']) as image:reference=image.convert('RGBA')
  template=red_mask(reference)
  outline=ImageChops.multiply(white_mask(reference),template.filter(ImageFilter.MaxFilter(9)))
  outline=ImageChops.multiply(outline,reference.getchannel('A').point(lambda v:255 if v>220 else 0))
  for color in [(225,100,90),(240,15,15)]:
   background=Image.new('RGBA',reference.size,color+(255,))
   _,red,white=artwork_score(background,template,outline)
   self.assertLess(white,.70)
   background.alpha_composite(reference)
   _,red,white=artwork_score(background,template,outline)
   self.assertGreaterEqual(red,.92);self.assertGreaterEqual(white,.70)

 def test_overlay_reuses_caption_pixels_and_copies_audio(self):
  from autocut.cta_policy import overlay_only
  ensure_plan(self.p);layer=self.folder/'existing-subtitle.png';Image.new('RGBA',(1080,1920)).save(layer)
  self.p['cta_preserve_text_layers']=[dict(layer=str(layer),layer_sha256=sha(layer),start=8,end=12)]
  original=copy.deepcopy(self.p);base=self.folder/'original.mp4';base.write_bytes(b'base');out=self.folder/'with-cta.mp4'
  def render(args,**kwargs):Path(args[-1]).write_bytes(b'rendered');return MagicMock(returncode=0)
  with patch('autocut.cta_policy.subprocess.run',side_effect=render) as ff,patch('autocut.item_pipeline.tts_job') as tts:
   overlay_only(self.p,base,out,self.folder/'overlay');args=ff.call_args.args[0]
   self.assertIn(str(layer),args);self.assertEqual(args[args.index('-c:a')+1],'copy')
   self.assertIn("gte(t,8)*lt(t,12)",args[args.index('-filter_complex')+1]);tts.assert_not_called()
  self.assertEqual(self.p,original);self.assertEqual(base.read_bytes(),b'base')

if __name__=='__main__':unittest.main()
