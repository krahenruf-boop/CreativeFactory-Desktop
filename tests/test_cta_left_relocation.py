"""Real pixels: legacy right CTA is removed, never hidden by a second arrow."""
import copy,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from autocut.context import RES,asset
from autocut.cta_policy import ensure_plan,verify,overlay_only
from autocut.cta_repair import relocate_from_source,text_signature
from app.s3.visual_preset import build_visual_plan
from app.s3.headless_media import render_plan
from app.s3.headless_runtime import sha,read
from app.s3.overlay_policy import lock_plan

class LeftRelocationTests(unittest.TestCase):
 def test_right_only_and_double_arrow_fail_then_source_rebuild_passes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);ff=str(RES/'runtime/media/bin/ffmpeg');source=root/'source.mp4';audio=root/'audio.wav'
   subprocess.run([ff,'-nostdin','-v','error','-f','lavfi','-i','color=c=0x305070:s=1080x1920:r=30:d=1',
    '-c:v','libx264','-threads','2','-pix_fmt','yuv420p',str(source)],check=True)
   subprocess.run([ff,'-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1',str(audio)],check=True)
   visual=build_visual_plan(1,'Título',[dict(start=0,end=1,text='Confere aqui')],asset=asset(),
    overrides=[dict(layer='cta',override_reason='PRODUCT_OCCLUSION',chosen_position=[.834,.65])])
   old=dict(rank='01',target_duration=1,source=dict(path=str(source),sha256=sha(source),source_end=1),
    audio=dict(audio_path=str(audio),sha256=sha(audio)),video_speed=1,audio_speed=1,source_ranges=[[0,1]],
    visual=visual,overlays=[],overlay_analysis={'status':'COMPLETE','reviewer':'fixture'},output=str(root/'old.mp4'),usage_scope='test')
   lock_plan(old);rendered=render_plan(old,old['output'],root/'old-render');evidence=read(rendered['evidence']);oldhash=sha(old['output'])
   p=copy.deepcopy(old);ensure_plan(p);p['cta_preserve_text_layers']=evidence['text_layers'];lock_plan(p)
   before=verify(p,old['output'],root/'before')
   self.assertEqual(before['status'],'FAIL');self.assertGreater(before['right_matched_frames'],20)
   both=root/'both.mp4';overlay_only(p,old['output'],both,root/'double')
   double=verify(p,both,root/'double-check')
   self.assertGreater(double['matched_frames'],20);self.assertIn('CTA_RIGHT_FORBIDDEN',double['issues'])
   with patch('autocut.item_pipeline.tts_job') as tts:
    result=relocate_from_source(p,old['output'],root/'fixed.mp4',root/'repair',evidence)
    tts.assert_not_called()
   self.assertEqual(result['cta_verification']['status'],'PASS')
   self.assertEqual(result['cta_verification']['right_matched_frames'],0)
   self.assertFalse((root/'repair/source-render.mp4').exists())
   self.assertEqual(text_signature(read(result['evidence'])),text_signature(evidence))
   def audio_hash(path):
    return subprocess.check_output([ff,'-nostdin','-v','error','-i',str(path),'-map','0:a:0','-c','copy','-f','hash','-hash','sha256','-'])
   self.assertEqual(audio_hash(old['output']),audio_hash(result['output']))
   self.assertEqual(sha(old['output']),oldhash)
   with patch('autocut.cta_repair.render_plan') as render:
    again=relocate_from_source(p,old['output'],root/'fixed.mp4',root/'repair',evidence)
    self.assertEqual(again['output_sha256'],result['output_sha256']);render.assert_not_called()

if __name__=='__main__':unittest.main()
