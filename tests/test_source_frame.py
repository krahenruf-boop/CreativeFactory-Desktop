import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from fastapi.testclient import TestClient
import autocut.context
from autocut.source_frame import POLICY, AUTO_POLICY, DEFAULT_POLICY, bind, canvas_box, filters, geometry, snapshot
from autocut.task_instructions import effective
from app.s3.headless_runtime import sha


def fixture(root, size=(180, 240)):
    im = Image.new('RGB', size, '#4779aa')
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, 15, size[1]-1), fill='#ef1515')
    draw.rectangle((size[0]-16, 0, size[0]-1, size[1]-1), fill='#15ef15')
    draw.rectangle((30, 0, size[0]-31, 15), fill='#eeee15')
    draw.rectangle((30, size[1]-16, size[0]-31, size[1]-1), fill='#15eeee')
    image = root / 'input.png'; im.save(image)
    path = root / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-loop', '1', '-i', str(image),
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                    '-t', '2', '-r', '30', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-y', str(path)], check=True)
    return dict(path=str(path), sha256=sha(path), width=size[0], height=size[1],
                duration=2., rank=1, filename=path.name, status='VALID')


class SourceFrameTests(unittest.TestCase):
    def test_full_foreground_edges_center_and_no_black_bars(self):
        for size in ((180, 240), (320, 180)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as d:
                root = Path(d); source = fixture(root, size)
                layout = geometry(source, 180, 320)
                graph = filters(source, '0:v:0', 180, 320, 'fit', POLICY)
                output = root / 'frame.png'
                subprocess.run(['ffmpeg', '-v', 'error', '-i', source['path'],
                                '-filter_complex', graph, '-map', '[fitout]',
                                '-frames:v', '1', '-y', str(output)], check=True)
                im = Image.open(output).convert('RGB'); x, y, w, h = layout['foreground']
                self.assertGreater(im.getpixel((3, 160))[0], 180)
                self.assertGreater(im.getpixel((176, 160))[1], 180)
                self.assertGreater(im.getpixel((90, round(y)+3))[0], 180)
                self.assertGreater(im.getpixel((90, round(y+h)-4))[2], 180)
                self.assertLess(sum(max(p)<24 for p in im.getdata()) / (180*320), .001)
                self.assertEqual(sha(source['path']), source['sha256'])
                self.assertEqual(2*y+h, 320)

    def test_portrait_passthrough_and_source_change_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            source = fixture(Path(d), (180, 320))
            self.assertIsNone(filters(source, '0:v:0', 180, 320, 'fit', POLICY))
            self.assertIsNone(filters({}, '0:v:0', 180, 320, 'fit', None))
            with self.assertRaisesRegex(ValueError, 'SOURCE_FRAME_INVALID'):
                filters(source, '0:v:0', 180, 320, 'fit', 'untrusted')
            source['sha256'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'SOURCE_HASH_CHANGED'):
                geometry(source, 180, 320)

    def test_plan_and_source_text_coordinates_share_foreground(self):
        with tempfile.TemporaryDirectory() as d:
            source = fixture(Path(d))
            plan = dict(source=copy.deepcopy(source), visual=dict(canvas=[1080,1920]),
                        task_overrides=dict(source_frame_mode=POLICY),
                        montage_segments=[copy.deepcopy(source)])
            bind(plan)
            self.assertEqual(plan['source']['source_frame_layout']['foreground'], [0,240.,1080,1440])
            self.assertEqual(plan['montage_segments'][0]['source_frame_mode'], POLICY)
            self.assertEqual(canvas_box(plan, [0,0,1,1]), [0,240,1080,1680])
            from autocut.original_text_cover import canvas_box as text_box
            self.assertEqual(text_box(plan, [.1,.1,.9,.2]), [108,384,972,528])
            self.assertEqual(source['sha256'], sha(source['path']))

    def test_default_submission_preserves_old_job_and_real_approval(self):
        from autocut.storage import Store
        from autocut.server import create_app
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'source.mp4').write_bytes(b'isolated-source')
            store = Store(root/'data')
            old, _ = store.create(dict(request_id='old-framing-fixture', product_name='Old',
                                      market='Brazil',mode='Transport'))
            store.update(old['id'],state='COMPLETED'); before = store.get(old['id'])
            class Manager:
                active={};item_pending=set()
                def enqueue(self, ident):pass
            client = TestClient(create_app(store,None,None,Manager(),'fixture',recover_on_startup=False),
                                base_url='http://127.0.0.1:9999', headers={'Authorization':'Bearer fixture'})
            self.assertEqual(client.post('/native/grant',json=dict(path=str(root))).status_code,200)
            spec = dict(request_id='new-framing-fixture', input_path=str(root),product_name='New')
            response = client.post('/jobs',json=spec)
            self.assertEqual(response.status_code,200,response.text)
            new = store.get(response.json()['id'])
            self.assertEqual(effective(new,1)['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(new['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(new['source_frame_policy'],DEFAULT_POLICY)
            self.assertEqual(effective(new,1)['title_max_lines'],2)
            self.assertEqual(effective(new,1)['title_duration'],5.)
            self.assertTrue(new['content_confirmation_required']);self.assertTrue(new['batch_review_required'])
            self.assertEqual(store.get(old['id']),before)
            self.assertEqual(client.post('/jobs',json=spec).json()['id'],new['id'])
            remix=dict(spec,request_id='new-remix-framing-fixture',mode='Remix',output_count=2)
            result=client.post('/jobs',json=remix)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(effective(store.get(result.json()['id']),1)['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(store.get(old['id']),before)
            spec.update(request_id='bad-framing-fixture',source_frame_mode='arbitrary')
            self.assertEqual(client.post('/jobs',json=spec).status_code,422)
            with self.assertRaisesRegex(ValueError,'INVALID_REQUEST'):
                snapshot({},dict(mode='unsupported',source_frame_mode=POLICY))
            client.close()

    def test_shared_feishu_submission_freezes_default_for_transport_and_remix(self):
        from autocut.job_submission import JobSubmission
        from autocut.storage import Store
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store=Store(root/'data');started=[]
            class Manager:
                def enqueue(self, ident):started.append(ident)
            submission=JobSubmission(store,Manager(),None,None)
            spec=dict(request_id='feishu-framing-default-fixture',input_path=str(root),
                      product_profile_id='general',product_name='Fixture',market='Brazil',
                      mode='Transport',title_approval_mode='auto',watermark_mode='handled',
                      script_reference_mode='smart',script_mode='AUTO',
                      submission_origin='feishu_task')
            job=submission.submit(spec,lambda p:None)
            self.assertEqual(effective(job,1)['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(job['source_frame_policy'],DEFAULT_POLICY)
            self.assertTrue(job['content_confirmation_required'])
            self.assertEqual(started,[job['id']])
            remix=submission.submit(dict(spec,request_id='feishu-remix-frame-default',mode='Remix',output_count=2),lambda p:None)
            self.assertEqual(effective(remix,1)['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(remix['source_frame_policy'],DEFAULT_POLICY)
            self.assertEqual(snapshot({},dict(mode='Remix'))['source_frame_mode'],AUTO_POLICY)
            original={'resolved_overrides_json':{'values':{'title_max_lines':2}}}
            saved=snapshot(original,dict(mode='Transport'))
            self.assertNotIn('source_frame_mode',original['resolved_overrides_json']['values'])
            self.assertEqual(snapshot(saved,dict(mode='Transport')),saved)

    def test_each_montage_source_uses_its_own_aspect_and_full_foreground(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);wide=root/'wide';portrait=root/'portrait';wide.mkdir();portrait.mkdir()
            first=fixture(wide,(180,240));second=fixture(portrait,(180,320))
            plan=dict(source=copy.deepcopy(first),visual=dict(canvas=[1080,1920]),
                      task_overrides=dict(source_frame_mode=POLICY),
                      montage_segments=[copy.deepcopy(first),copy.deepcopy(second)])
            bind(plan)
            a,b=plan['montage_segments']
            self.assertEqual(a['source_frame_layout']['foreground'],[0,240.,1080,1440])
            self.assertNotIn('source_frame_layout',b)
            self.assertEqual(a['source_frame_mode'],b['source_frame_mode'])
            self.assertEqual(sha(first['path']),first['sha256'])
            self.assertEqual(sha(second['path']),second['sha256'])

    def test_real_preflight_and_final_share_framing_with_montage(self):
        from autocut.pipeline import make_plan
        from app.s3.headless_media import render_plan,probe
        from app.s3.overlay_policy import lock_plan
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=fixture(root);(root/'plans').mkdir()
            audio=dict(path=source['path'],sha256=source['sha256'],duration=2.)
            p=make_plan(source,audio,{},dict(title_y=.17,subtitle_y=.78,override_reason='NONE'),
                        'Um carinho especial',root,root/'final.mp4',dict(usage_scope='test',language='pt-BR'),
                        task_overrides=dict(source_frame_mode=POLICY,title_max_lines=2),music_only=True)
            for montage in (False, True):
                plan=copy.deepcopy(p)
                if montage:
                    plan.pop('source_ranges');plan['montage_segments']=[dict(source,source_start=0.,source_end=2.,source_duration=2.,output_start=0.,output_end=2.,speed=1.)]
                    bind(plan);lock_plan(plan)
                for preflight in (False,True):
                    name=f'{montage}-{preflight}'
                    result=render_plan(plan,root/(name+'.mp4'),root/name,preflight=preflight)
                    video=next(s for s in probe(root/(name+'.mp4'))['streams'] if s['codec_type']=='video')
                    self.assertEqual((video['width'],video['height']),(540,960) if preflight else (1080,1920))
                    self.assertTrue(result)
                    image=root/(name+'.png')
                    subprocess.run(['ffmpeg','-v','error','-ss','1','-i',str(root/(name+'.mp4')),
                                    '-frames:v','1','-y',str(image)],check=True)
                    im=Image.open(image).convert('RGB');w,h=im.size
                    self.assertGreater(im.getpixel((round(w*.025),h//2))[0],180)
                    self.assertGreater(im.getpixel((round(w*.975),h//2))[1],180)
            self.assertEqual(len(p['visual']['title_layout']['lines']),2)
            self.assertEqual(sha(source['path']),source['sha256'])


if __name__=='__main__':unittest.main()
