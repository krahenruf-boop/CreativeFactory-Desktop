"""Small isolated render proving CTA is underneath captions; no AI/jobs."""
import copy
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageChops
from autocut.context import HOME, RES, asset
from autocut.cta_policy import ensure_plan, verify
from autocut.production_policy import classify
from app.s3.headless_runtime import sha
from app.s3.headless_media import render_plan
from app.s3.overlay_policy import lock_plan
from app.s3.visual_preset import build_visual_plan, visual_inputs


class CTATextLayersTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.ff=str(RES/'runtime/media/bin/ffmpeg')
  self.visual=build_visual_plan(2,'Título',[
   dict(start=0,end=2,text='Mantém sua dignidade dentro da calça')],asset=asset(),canvas=(1080,1920),
   overrides=[dict(layer='subtitle',override_reason='CTA_SUBTITLE_COLLISION',chosen_position=[.5,.68])])

 def test_overlap_allowed_only_with_subtitles_on_top(self):
  with self.assertRaisesRegex(ValueError,'CTA_SUBTITLE_COLLISION'):
   visual_inputs(self.visual,self.root/'strict',HOME/'user_assets')
  _,_,e=visual_inputs(self.visual,self.root/'allowed',HOME/'user_assets',allow_subtitle_overlap=True)
  self.assertTrue(e['cta']);self.assertEqual(len(e['text_layers']),2)
  broken=copy.deepcopy(self.visual);broken['cta']['asset']['file_path']=str(self.root/'missing.png')
  with self.assertRaises(Exception):visual_inputs(broken,self.root/'broken',HOME/'user_assets',allow_subtitle_overlap=True)

 def test_overlap_only_is_warning_but_real_failures_stay_failures(self):
  allowed=dict(status='FAIL',severity='SEVERE',issues=['CTA_SUBTITLE_COLLISION'],product_preserved=True)
  self.assertEqual(classify(allowed)['status'],'WARNING')
  for issue in ['UNREADABLE_SUBTITLES','PRODUCT_OBSCURED','NO_VIDEO']:
   self.assertEqual(classify(dict(allowed,issues=[*allowed['issues'],issue]))['status'],'FAIL')
  self.assertEqual(classify(allowed,technical=True)['status'],'FAIL')

 def test_real_short_render_captions_cover_arrow_and_visible_arrow_verifies(self):
  source=self.root/'source.mp4';audio=self.root/'silence.wav'
  subprocess.run([self.ff,'-nostdin','-v','error','-f','lavfi','-i','color=c=0x305070:s=1080x1920:r=30:d=2','-c:v','libx264','-threads','2','-pix_fmt','yuv420p',str(source)],check=True)
  subprocess.run([self.ff,'-nostdin','-v','error','-f','lavfi','-i','anullsrc=r=48000:cl=mono','-t','2',str(audio)],check=True)
  plan=dict(rank='01',source=dict(path=str(source),sha256=sha(source),source_end=2),audio=dict(audio_path=str(audio),sha256=sha(audio)),
   source_ranges=[[0,2]],video_speed=1,audio_speed=1,target_duration=2,visual=self.visual,overlays=[],overlay_analysis={'status':'COMPLETE','reviewer':'fixture'},
   output=str(self.root/'output.mp4'),usage_scope='test')
  ensure_plan(plan);lock_plan(plan)
  result=render_plan(plan,plan['output'],self.root/'render')
  evidence=json.loads(Path(result['evidence']).read_text());self.assertEqual(evidence['cta_layer_order'],'BELOW_TEXT')
  layers=evidence['text_layers'];sub=next(l for l in layers if l['kind']=='subtitle');c=evidence['cta']
  at=.4;top=round(c['base_y']+c['amplitude_px']*math.sin(2*math.pi*(at-c['start'])/c['period']));left=round(c['x'])
  with Image.open(sub['layer']) as im:caption=im.convert('RGBA')
  with Image.open(c['path']) as im:arrow=im.convert('RGBA')
  positioned=Image.new('RGBA',caption.size);positioned.alpha_composite(arrow,(left,top))
  overlap=ImageChops.multiply(caption.getchannel('A').point(lambda v:255 if v>250 else 0),positioned.getchannel('A').point(lambda v:255 if v>250 else 0))
  self.assertIsNotNone(overlap.getbbox(),'fixture must really overlap')
  frame=self.root/'frame.png'
  subprocess.run([self.ff,'-nostdin','-v','error','-ss',str(at),'-i',plan['output'],'-frames:v','1',str(frame)],check=True)
  with Image.open(frame) as im:actual=im.convert('RGB')
  differences=[]
  x0,y0,x1,y1=overlap.getbbox()
  for y in range(y0,y1):
   for x in range(x0,x1):
    if overlap.getpixel((x,y))==255:
     expected=caption.getpixel((x,y));pixel=actual.getpixel((x,y))
     differences.append(sum(abs(pixel[i]-expected[i]) for i in range(3))/3)
  self.assertGreater(len(differences),10);self.assertLess(sum(differences)/len(differences),20,'caption pixels must be above arrow')
  plan['cta_preserve_text_layers']=layers
  self.assertEqual(verify(plan,plan['output'],self.root/'verify')['status'],'PASS')
  self.assertEqual(verify(plan,source,self.root/'missing-arrow')['status'],'FAIL')

 def test_explicit_cta_off_still_builds_text_layers(self):
  self.visual['cta']['enabled']=False
  self.visual['user_revision']={'omit_added_cta':True}
  _,_,e=visual_inputs(self.visual,self.root/'off',HOME/'user_assets',allow_subtitle_overlap=True)
  self.assertFalse(e['cta']['enabled']);self.assertEqual(len(e['text_layers']),2)

if __name__=='__main__':unittest.main()
