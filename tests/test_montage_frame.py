import tempfile,subprocess,unittest
from pathlib import Path
from unittest.mock import patch
import threading
from PIL import Image,ImageDraw
import autocut.context
from autocut.montage_frame import frame_filters,validate
from autocut.automatic_finish import repaired_visual
from autocut.ai_recovery import render_evidence
from app.s3.headless_runtime import sha,write
from app.s3.visual_preset import build_visual_plan,validate_visual_plan

class MontageFrameTests(unittest.TestCase):
 def test_checked_mp4_record_reconciliation_uses_valid_transition(self):
  from autocut.storage import Store
  from autocut.final_record import reconcile
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);output=root/'checked.mp4'
   subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=yellow:s=180x320:r=30','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(output)],check=True)
   store=Store(root/'isolated');job,_=store.create(dict(request_id='record-fixture',product_name='Girassol',market='Brazil',mode='Remix',batch_review=dict(phase='RUNNING',ranks=['09'],receipts={})))
   jid=job['id'];work=store.home/'jobs'/jid/'work';digest=sha(output);title='Um girassol no carro pra lembrar de quem você ama';plan=dict(rank='09',source=dict(path=str(output),sha256=digest),audio=dict(path=str(output),audio_path=str(output),sha256=digest),output=str(output),final_script=None,title=title,voiceover_mode='MUSIC_ONLY',visual=dict(subtitle=dict(captions=[])))
   path=work/'plan.json';write(path,plan);store.item(jid,'09',status='FAILED',issues=['FINAL_NOT_READY'],script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,voiceover_mode='MUSIC_ONLY',final_title=dict(text=title,language='pt-BR'),final_title_language='pt-BR',output=str(output),plan=str(path))
   rendered=dict(status='PASS',output=str(output),output_sha256=digest);qa=dict(status='PASS',technical=dict(status='PASS',full_decode_count=1,output_sha256=digest),visual=dict(status='PASS',severity='NONE',issues=[]),cta=dict(status='PASS',output_sha256=digest))
   store.done(jid,'FINAL_RENDERED','09','render0',rendered);store.done(jid,'QA_DONE','09','qa1',qa);write(work/'render/render0/result.json',rendered);write(work/'qa/09/qa.json',qa['technical'])
   class Manager:pass
   m=Manager();m.store=store;m.lock=threading.RLock();m.item_pending=set()
   reconcile(m,jid,'09');self.assertEqual(store.get(jid)['items'][0]['status'],'PASS');self.assertEqual(store.get(jid)['state'],'COMPLETED');self.assertEqual(sha(output),digest)
   store.item(jid,'09',status='FAILED',issues=['FINAL_NOT_READY']);reconcile(m,jid,'09');self.assertEqual(store.get(jid)['items'][0]['status'],'PASS');self.assertEqual(sha(output),digest)

 def test_worker_render_contract_does_not_drift_mid_render(self):
  from app.s3.headless_media import render_contract
  render_contract.cache_clear()
  with patch('app.s3.headless_media.inspect.getsource',side_effect=lambda f:f.__name__+str(id(f))) as source:
   first=render_contract();self.assertEqual(render_contract(),first);self.assertEqual(source.call_count,6)
  render_contract.cache_clear()

 def test_black_borders_removed_and_whole_foreground_preserved(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);im=Image.new('RGB',(180,320),'black');draw=ImageDraw.Draw(im);draw.rectangle((0,40,179,279),fill=(20,130,230));draw.rectangle((0,100,20,200),fill=(230,20,20));draw.rectangle((159,100,179,200),fill=(20,230,20));im.save(root/'input.png')
   source=root/'source.mp4';subprocess.run(['ffmpeg','-v','error','-loop','1','-i',str(root/'input.png'),'-t','2','-r','30','-pix_fmt','yuv420p','-y',str(source)],check=True)
   segment=dict(path=str(source),sha256=sha(source),speed=1.,letterbox_crop=[0,40,180,240])
   graph=frame_filters(segment,0,60,180,320,0)
   subprocess.run(['ffmpeg','-v','error','-i',str(source),'-filter_complex',graph,'-map','[mix0]','-frames:v','1','-y',str(root/'result.png')],check=True)
   out=Image.open(root/'result.png').convert('RGB');self.assertLess(sum(max(p)<24 for p in out.getdata())/(180*320),.01)
   self.assertGreater(out.getpixel((10,150))[0],180);self.assertGreater(out.getpixel((169,150))[1],180)
   self.assertEqual(sha(source),segment['sha256'])
   for rect in [[0,40,180,400],[0.,40,180,240],[-2,40,180,240]]:
    segment['letterbox_crop']=rect
    with self.assertRaisesRegex(ValueError,'MONTAGE_FRAME_INVALID'):validate(segment)
   segment['letterbox_crop']=[0,40,180,240];segment['sha256']='changed'
   with self.assertRaisesRegex(ValueError,'MONTAGE_SOURCE_HASH_CHANGED'):validate(segment)

 def test_automatic_repair_keeps_exact_two_line_user_policy(self):
  with tempfile.TemporaryDirectory() as d:
   f=Path(d)/'asset';f.write_bytes(b'fixture');asset=dict(id='fixture',file_path=str(f),sha256=sha(f));text='Presente romântico simples: girassol que enfeita o carro e a mesa';v=build_visual_plan(35.3,text,[],asset=asset)
   p=dict(visual=v,title=text,target_duration=35.3,task_overrides=dict(title_max_lines=2),title_enabled=True)
   fixed=repaired_visual(p);self.assertEqual(len(fixed['title_layout']['lines']),2);self.assertEqual(fixed['title']['max_lines'],2);self.assertEqual(fixed['title']['end'],5)
   self.assertEqual(' '.join(fixed['title_layout']['lines']),text);self.assertEqual(validate_visual_plan(fixed),[])

 def test_music_only_recovery_reuses_frozen_audio_and_title(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);s=root/'source';s.write_bytes(b'source');a=root/'audio';a.write_bytes(b'audio');plan=dict(rank='08',final_script=None,title='Um girassol de amor',source=dict(path=str(s),sha256=sha(s)),audio=dict(path=str(a),audio_path=str(a),sha256=sha(a)),output=str(root/'not-rendered.mp4'));path=root/'plan.json';write(path,plan)
   item=dict(rank='08',plan=str(path),voiceover_mode='MUSIC_ONLY',final_title=dict(text=plan['title'],language='pt-BR'))
   self.assertEqual(render_evidence(item),plan)

if __name__=='__main__':unittest.main()
