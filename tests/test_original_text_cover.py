import copy
import tempfile
import unittest
import json
from unittest.mock import MagicMock,patch
from pathlib import Path
from PIL import Image
from autocut import context
from autocut.original_text_cover import (bind, evaluate, validate, map_interval,
    apply_layers, review_issues, canvas_box, CODES)
from app.s3.headless_runtime import sha
from app.s3.overlay_policy import lock_plan, render_gate
from app.s3.visual_preset import load_preset


class OriginalTextCoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);p=self.folder/'evidence.png'
        Image.new('RGB',(10,10),'white').save(p)
        self.p=dict(source=dict(sha256=sha(p),width=720,height=1280,duration=20,source_end=20),
            source_ranges=[[0,20]],video_speed=1.25,target_duration=16,
            title='Cinto cravando na pele? Tem solução',final_title_language='pt-BR',target_language='pt-BR',
            visual=dict(canvas=[1080,1920],title=dict(load_preset()['title'],start=0,end=5)),
            final_script='approved copy',audio={'sha256':'cached-paid-tts'},audio_speed=1.18,
            overlay_analysis={'status':'COMPLETE'},overlays=[])
        self.region=dict(id='old-sub',role='SUBTITLE',original_bbox=[.25,.7,.7,.74],
            bbox=[.25,.7,.7,.74],source_interval=[2,6],classified_from_video=True)
        self.spec=dict(source_sha256=sha(p),evidence=[dict(path=str(p),sha256=sha(p))],
            subject_scan_complete=True,reviewed_intervals=[[0,20]],
            protected_regions=[dict(kind='face',bbox=[.2,.1,.6,.3],start=0,end=20)],regions=[self.region])

    def test_trim_split_speed_jumpcut(self):
        self.p.update(source_ranges=[[0,4],[10,20]],target_duration=11.2)
        actual=map_interval(self.p,3,12);self.assertEqual(len(actual),2)
        for got,want in zip(sum(actual,[]),[2.4,3.2,3.2,4.8]):self.assertAlmostEqual(got,want)

    def test_montage_never_spills_into_another_source(self):
        s=self.p['source']['sha256']
        self.p['montage_segments']=[dict(sha256='other',source_start=0,source_end=20,speed=1,output_start=0,output_end=20),
            dict(sha256=s,source_start=10,source_end=20,speed=2,output_start=10,output_end=15)]
        self.assertEqual(map_interval(self.p,11,14),[[10.5,12.]])

    def test_region_change_and_gaps_preserve_timing(self):
        other=dict(self.region,id='next',bbox=[.3,.69,.65,.73],original_bbox=[.3,.69,.65,.73],source_interval=[8,9])
        self.spec['regions'].append(other);r=bind(self.p,self.spec)
        self.assertEqual([(x['start'],x['end']) for x in r['events']],[(1.6,4.8),(6.4,7.2)])

    def test_brand_and_environment_not_removed(self):
        self.spec['regions']=[{'role':'PRODUCT_BRANDING'},{'role':'ENVIRONMENT_INFORMATION'}]
        self.assertEqual(evaluate(self.p,self.spec)['events'],[])

    def test_no_subject_observations_is_not_safe(self):
        for change in ({'subject_scan_complete':False},{'reviewed_intervals':[[0,1],[3,20]]}):
            with self.assertRaisesRegex(ValueError,'TEXT_SUBJECT_EVIDENCE_REQUIRED'):
                bind(self.p,dict(self.spec,**change))

    def test_cover_face_hands_product_rejected(self):
        for kind in ('face','hands','product'):
            self.spec['protected_regions']=[dict(kind=kind,bbox=[.3,.6,.6,.9],start=3,end=4)]
            with self.assertRaisesRegex(ValueError,'COVER_SUBJECT_OCCLUSION'):bind(self.p,self.spec)

    def test_original_card_is_not_face_exemption(self):
        self.region.update(role='TITLE',original_bbox=[.2,.12,.7,.16],bbox=[.2,.12,.7,.16])
        with self.assertRaisesRegex(ValueError,'COVER_SUBJECT_OCCLUSION'):bind(self.p,self.spec)

    def test_padding_too_large_and_uncovered_edge_rejected(self):
        self.region['bbox']=[.2,.7,.7,.74]
        with self.assertRaisesRegex(ValueError,'WHITE_COVER_TOO_LARGE'):bind(self.p,self.spec)
        self.region['bbox']=[.27,.7,.7,.74]
        with self.assertRaisesRegex(ValueError,'OLD_TEXT_RESIDUE'):bind(self.p,self.spec)

    def test_evidence_hash_and_plan_lock(self):
        bind(self.p,self.spec);lock_plan(self.p)
        self.assertEqual(render_gate(self.p),[])
        self.p['original_text_cleanup']['events'][0]['end']+=1
        self.assertIn('OVERLAY_PLAN_CHANGED_AFTER_PREVIEW',render_gate(self.p))
        with self.assertRaisesRegex(ValueError,'TEXT_CLEANUP_PLAN_CHANGED'):validate(self.p)
        bind(self.p,self.spec);Path(self.spec['evidence'][0]['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError,'TEXT_EVIDENCE_CHANGED'):validate(self.p)

    def test_exact_opaque_raster(self):
        bind(self.p,self.spec);e={'text_layers':[]};apply_layers(self.p,e,self.folder)
        m=e['text_layers'][0]
        with Image.open(m['layer']) as im:
            self.assertEqual(list(im.getbbox()),m['bbox'])
            l,t,r,b=m['bbox'];self.assertEqual(im.getpixel((l,t)),(255,255,255,255))
            self.assertEqual(im.getpixel((l-1,t)),(0,0,0,0))

    def test_new_subtitle_dark_on_plate_without_changed_words_or_times(self):
        path=self.folder/'caption.png';Image.new('RGBA',(1080,1920),(255,255,255,0)).save(path)
        m=dict(kind='subtitle',text='pt-BR approved',bbox=[280,1350,600,1400],layer=str(path),start=0,end=8)
        bind(self.p,self.spec);e={'text_layers':[m]};apply_layers(self.p,e,self.folder)
        rows=[x for x in e['text_layers'] if x['kind']=='subtitle']
        self.assertEqual([(x['start'],x['end']) for x in rows],[(0,1.6),(1.6,4.8),(4.8,8)])
        self.assertEqual(rows[1]['foreground'],'#171717');self.assertTrue(all(x['text']==m['text'] for x in rows))

    def test_existing_approved_title_reused(self):
        self.region.update(role='TITLE',bbox=[.18,.12,.79,.16],original_bbox=[.18,.12,.79,.16])
        self.spec['protected_regions']=[];bind(self.p,self.spec)
        e={'text_layers':[]};apply_layers(self.p,e,self.folder)
        self.assertEqual(e['text_layers'][0]['text'],self.p['title'])

    def test_copy_audio_and_speed_unchanged(self):
        old=copy.deepcopy(self.p);bind(self.p,self.spec);apply_layers(self.p,{'text_layers':[]},self.folder)
        self.assertEqual({k:v for k,v in self.p.items() if k!='original_text_cleanup'},old)

    def test_all_requested_qa_findings_block_even_warning(self):
        bind(self.p,self.spec)
        for code in CODES:
            r=dict(source_text=True,overlay=True,product_preserved=True,severity='WARNING',issues=[code])
            self.assertIn(code,review_issues(self.p,r))
        self.assertIn('ORIGINAL_SUBTITLE_VISIBLE',review_issues(self.p,dict(source_text=False)))
        self.assertEqual(review_issues({},dict(source_text=False)),[])

    def test_aspect_fill_crop_coordinates(self):
        self.p['source'].update(width=1920,height=1080)
        self.assertEqual(canvas_box(self.p,[.45,.7,.55,.74]),[369,1344,711,1421])

    def test_normal_plan_unchanged(self):
        e={'text_layers':[{'kind':'title'}]};old=copy.deepcopy(e);apply_layers(self.p,e,self.folder)
        self.assertEqual(e,old)

    def test_real_finish_never_waives_text_failure_or_enters_review(self):
        from autocut.automatic_finish import finish
        for failed_phase in ('preflight','final'):
            p=copy.deepcopy(self.p);p.update(rank='04',voiceover_mode='MUSIC_ONLY',title_enabled=False,
                original_text_cleanup={'strategy':'ORIGINAL_TEXT_CLEANUP_V1'},output=str(self.folder/'evidence.png'))
            p['source']['path']=str(self.folder/'evidence.png')
            m=MagicMock();job={'language':'pt-BR','market':'BR'};item={'rank':'04','status':'EDITING'}
            good=dict(status='PASS',source_text=True,overlay=True,product_preserved=True,title=True,subtitle=True,cta=True,watermark=True)
            bad=dict(good,source_text=False,severity='WARNING',issues=['ORIGINAL_SUBTITLE_VISIBLE'])
            m.judge.side_effect=[bad] if failed_phase=='preflight' else [good,bad]
            with patch('autocut.item_review.get_item',return_value=(job,item)), \
                 patch('autocut.automatic_finish.identity',return_value='locked'), \
                 patch('autocut.automatic_finish.preflight_job') as preflight, \
                 patch('autocut.automatic_finish.approve_preflight') as approve, \
                 patch('autocut.automatic_finish.preflight_review_context',create=True), \
                 patch('autocut.pipeline.preflight_review_context',return_value={}), \
                 patch('autocut.automatic_finish.final_job') as final, \
                 patch('autocut.automatic_finish.qa_job') as qa, \
                 patch('autocut.automatic_finish.final_ready') as pass_gate:
                preflight.return_value.result.return_value=dict(contact_sheet='fixture',output_sha256='fixture')
                final.return_value.result.return_value={'status':'PASS'}
                qa.return_value.result.return_value={'status':'PASS'}
                finish(m,'job',MagicMock(),job,[p],self.folder)
                if failed_phase=='preflight':approve.assert_not_called();final.assert_not_called()
                pass_gate.assert_not_called()
            calls=[c.kwargs for c in m.store.item.call_args_list]
            self.assertEqual(calls[-1]['status'],'FAILED')
            self.assertEqual(calls[-1]['issues'],['ORIGINAL_SUBTITLE_VISIBLE'])
            self.assertFalse(any(c.get('status') in ('PASS','REVIEW_REQUIRED') for c in calls))

    def test_in_cover_reflow_uses_cached_alignment_no_words_dropped(self):
        self.p['final_script']='Cinto muito confortável'
        self.p['audio_speed']=1
        self.p['visual']['subtitle']=load_preset()['subtitle']
        text=self.p['final_script'];path=self.folder/'alignment.json'
        a={'characters':list(text),'character_start_times_seconds':[2+i*.1 for i in range(len(text))],
            'character_end_times_seconds':[2+(i+1)*.1 for i in range(len(text))]}
        path.write_text(json.dumps({'alignment':a}))
        self.p['audio']['alignment_path']=str(path)
        self.spec['evidence'].append(dict(path=str(path),sha256=sha(path)))
        self.region.update(subtitle_layout='IN_COVER',source_interval=[0,10],bbox=[.15,.7,.8,.76],original_bbox=[.15,.7,.8,.76])
        bind(self.p,self.spec);e={'text_layers':[]};apply_layers(self.p,e,self.folder)
        rows=[x for x in e['text_layers'] if x['kind']=='subtitle']
        self.assertEqual(' '.join(x['text'] for x in rows),text)
        self.assertEqual(rows[0]['start'],2.)
        self.assertTrue(all(x['foreground']=='#171717' and x['font_loaded']=='Poppins Bold' for x in rows))
        self.assertTrue(all(a['end']<=b['start'] for a,b in zip(rows,rows[1:])))
        self.p['final_script']='Different approved text'
        with self.assertRaisesRegex(ValueError,'ALIGNMENT_TEXT_MISMATCH'):apply_layers(self.p,{'text_layers':[]},self.folder)


if __name__=='__main__':unittest.main()
