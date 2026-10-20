"""Future-job narrow framing reaches inspection, paid preflight and real export."""
import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import autocut.context
from PIL import Image
from autocut.source_frame import (POLICY, AUTO_POLICY, AUTO_HEIGHT_POLICY, HEIGHT_POLICY,
                                 geometry, filters, bind, snapshot, canvas_box)
from autocut.source_library import inspect_source, POLICY as PREFLIGHT_POLICY
from autocut.production_preflight import before_tts
from app.s3.headless_runtime import sha
from test_source_frame import fixture


class SlightNarrowTests(unittest.TestCase):
    def test_known_sizes_auto_fit_full_height_but_legacy_policy_is_unchanged(self):
        for size,expected in [((540,962),[2.,0,1076,1920]),((480,854),[1.,0,1078,1920])]:
            with self.subTest(size=size),tempfile.TemporaryDirectory() as d:
                source=fixture(Path(d),size)
                layout=geometry(source,1080,1920,AUTO_POLICY)
                self.assertEqual(layout,dict(foreground=expected,canvas=[1080,1920],policy=AUTO_HEIGHT_POLICY))
                with self.assertRaisesRegex(ValueError,'SOURCE_FRAME_TOO_TALL'):geometry(source,1080,1920,POLICY)
                self.assertEqual(sha(source['path']),source['sha256'])

    def test_one_percent_boundary_rotation_and_sample_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as d:
            source=fixture(Path(d))
            for video,allowed in [(dict(width=1782,height=3200),True),
                                  (dict(width=1780,height=3200),False),
                                  (dict(width=962,height=540,tags={'rotate':'90'}),True),
                                  (dict(width=540,height=962,sample_aspect_ratio='2:1'),True)]:
                with self.subTest(video=video),patch('app.s3.headless_media.probe',return_value={'streams':[dict(codec_type='video',**video)]}):
                    if allowed:self.assertIsNotNone(geometry(source,1080,1920,AUTO_POLICY))
                    else:
                        with self.assertRaisesRegex(ValueError,'SOURCE_FRAME_TOO_TALL'):geometry(source,1080,1920,AUTO_POLICY)
            exact=fixture(Path(d),(180,320));self.assertIsNone(geometry(exact,1080,1920,AUTO_POLICY))

    def test_new_snapshots_upgrade_old_form_values_and_per_item_rules_without_mutating_input(self):
        original=dict(resolved_overrides_json=dict(values={'title_max_lines':2},per_item_overrides=[dict(rank_start=1,rank_end=2,values={'source_frame_mode':POLICY})]))
        before=copy.deepcopy(original)
        for mode in ['Transport','Remix']:
            result=snapshot(original,dict(mode=mode,source_frame_mode=POLICY))
            self.assertEqual(result['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(result['resolved_overrides_json']['per_item_overrides'][0]['values']['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(snapshot(result,dict(mode=mode)),result)
        self.assertEqual(original,before)

    def test_inspection_cache_and_before_tts_do_not_mark_slight_narrow_as_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=fixture(root,(480,854));legacy=inspect_source(root,source)
            self.assertEqual(legacy['frame_issue'],'SOURCE_FRAME_TOO_TALL')
            cached={p:p.read_bytes() for p in (root/'cache/source-library').glob('*.json')}
            current=inspect_source(root,source,AUTO_POLICY)
            self.assertIsNone(current['frame_issue']);self.assertEqual(current['frame_layout']['policy'],AUTO_HEIGHT_POLICY)
            for path,data in cached.items():self.assertEqual(path.read_bytes(),data)
            with patch('autocut.source_library.subprocess.run',side_effect=AssertionError('cached analysis must be reused')):
                self.assertEqual(inspect_source(root,source,AUTO_POLICY),current)
            job=dict(id='fixture',production_preflight_policy=PREFLIGHT_POLICY,source_frame_mode=AUTO_POLICY)
            item=dict(rank='01');store=MagicMock()
            with patch('autocut.production_preflight.duration_view',return_value={'status':'READY'}):
                self.assertTrue(before_tts(store,job,item,{},'scope',[],[source]))
            self.assertTrue(all('status' not in call.kwargs for call in store.item.call_args_list))
            self.assertEqual(sha(source['path']),source['sha256'])

    def test_real_preview_final_and_montage_preserve_all_four_foreground_edges(self):
        from autocut.pipeline import make_plan
        from app.s3.headless_media import render_plan,probe
        from app.s3.overlay_policy import lock_plan
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=fixture(root,(540,962));wide_dir=root/'wide';wide_dir.mkdir();wide=fixture(wide_dir)
            (root/'plans').mkdir();audio=dict(path=source['path'],sha256=source['sha256'],duration=2.)
            plan=make_plan(source,audio,{},dict(title_y=.17,subtitle_y=.78,override_reason='NONE'),
                'Um carinho especial',root,root/'out.mp4',dict(usage_scope='test',language='pt-BR'),
                task_overrides=dict(source_frame_mode=AUTO_POLICY,title_max_lines=2),music_only=True)
            self.assertEqual(plan['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(canvas_box(plan,[0,0,1,1]),[2,0,1078,1920])
            for montage in [False,True]:
                p=copy.deepcopy(plan)
                if montage:
                    p.pop('source_ranges');p['montage_segments']=[dict(s,source_start=0.,source_end=1.,source_duration=2.,output_start=float(i),output_end=float(i+1),speed=1.) for i,s in enumerate([source,wide])]
                    bind(p);lock_plan(p)
                    self.assertEqual(p['montage_segments'][0]['source_frame_layout']['policy'],AUTO_HEIGHT_POLICY)
                    self.assertEqual(p['montage_segments'][1]['source_frame_layout']['policy'],POLICY)
                for preview in [False,True]:
                    name=f'{montage}-{preview}';output=root/(name+'.mp4');render_plan(p,output,root/name,preflight=preview)
                    info=next(s for s in probe(output)['streams'] if s['codec_type']=='video');self.assertEqual((info['width'],info['height']),(540,960) if preview else (1080,1920))
                    frame=root/(name+'.png');subprocess.run(['ffmpeg','-v','error','-ss','0.4','-i',str(output),'-frames:v','1','-y',str(frame)],check=True)
                    im=Image.open(frame).convert('RGB');w,h=im.size
                    for point,channel in [((round(w*.015),h//2),0),((round(w*.985),h//2),1),((w//2,4),0),((w//2,h-5),2)]:
                        self.assertGreater(im.getpixel(point)[channel],180)
                    self.assertGreater(max(im.getpixel((0,h//2))),24)
                    self.assertGreater(max(im.getpixel((w-1,h//2))),24)
                    decoded=subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(output),'-f','null','-'],capture_output=True);self.assertEqual(decoded.returncode,0)
            self.assertEqual(sha(source['path']),source['sha256']);self.assertEqual(sha(wide['path']),wide['sha256'])


if __name__=='__main__':unittest.main()
