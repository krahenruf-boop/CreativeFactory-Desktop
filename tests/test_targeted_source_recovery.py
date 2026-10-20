"""Scoped recovery guards; all data and provider calls are isolated fixtures."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import autocut.context
from autocut.storage import Store
from autocut.source_frame import POLICY, AUTO_POLICY, HEIGHT_POLICY, bind, geometry, snapshot, filters
from autocut.source_frame_recovery import request
from autocut.source_asr_recovery import recover, VERSION as ASR_VERSION
from autocut.ownership import item_state
from app.s3.headless_runtime import sha, write, fingerprint
from test_source_frame import fixture
from test_review_flow import ReviewTests, ANALYSIS, future, BR


class FrameExceptionTests(unittest.TestCase):
    def pre_tts_fixture(self, root):
        from autocut.source_library import POLICY as PREFLIGHT_POLICY
        source=fixture(root,(180,322));store=Store(root/'data')
        job,_=store.create(dict(request_id='pre-tts-frame',product_name='RGB',mode='Transport'))
        ident=job['id']
        store.update(ident,source_frame_mode=POLICY,production_preflight_policy=PREFLIGHT_POLICY,batch_review={'phase':'RUNNING'})
        store.item(ident,7,status='REVIEW_REQUIRED',issues=['SOURCE_FRAME_TOO_TALL'],source_snapshot=source,
                   analysis={'ok':True},script_status='USER_APPROVED',title_status='USER_APPROVED',
                   content_confirmed=True,final_script='Approved speech.',final_title='Approved title.')
        manager=MagicMock();manager.store=store;manager.lock=threading.RLock();manager.control.busy.return_value=False
        return store,manager,ident,source

    def test_pre_tts_frame_guard_recovers_without_creating_audio_or_approving(self):
        with tempfile.TemporaryDirectory() as d:
            store,manager,ident,source=self.pre_tts_fixture(Path(d));before=copy.deepcopy(store.get(ident)['items'][0])
            request(manager,ident,7,['07']);request(manager,ident,7,['07'])
            job=store.get(ident);item=job['items'][0]
            manager.enqueue_batch.assert_called_once_with(ident,[('07','SCRIPT')])
            self.assertEqual(job['source_frame_exceptions'],{source['sha256']:HEIGHT_POLICY})
            self.assertFalse(item.get('audio_result'));self.assertFalse(item.get('plan'))
            for key in ['final_script','final_title','script_status','title_status','content_confirmed']:
                self.assertEqual(item[key],before[key])
            with store.connect() as db:
                self.assertFalse(db.execute("SELECT 1 FROM checkpoints WHERE job_id=? AND stage='TTS_DONE'",(ident,)).fetchone())

    def test_missing_paid_audio_cannot_enter_pre_tts_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            store,manager,ident,_=self.pre_tts_fixture(Path(d))
            store.done(ident,'TTS_DONE','07','paid-request',dict(path='missing.mp3',sha256='paid-sha'))
            before=copy.deepcopy(store.get(ident))
            with self.assertRaisesRegex(ValueError,'RECOVERY_CACHE_INVALID'):request(manager,ident,7,['07'])
            self.assertEqual(store.get(ident),before);manager.enqueue_batch.assert_not_called()

    def test_pre_tts_recovery_requires_guard_state_and_real_approvals(self):
        for update,expected in [({'production_preflight_policy':None},'RECOVERY_CACHE_INVALID'),
                                ({'state':'PAUSED'},'STATE_TRANSITION_REJECTED'),
                                ({'state':'STOPPED'},'STATE_TRANSITION_REJECTED')]:
            with self.subTest(update=update),tempfile.TemporaryDirectory() as d:
                store,manager,ident,_=self.pre_tts_fixture(Path(d));store.update(ident,**update)
                with self.assertRaisesRegex(ValueError,expected):request(manager,ident,7,['07'])
                manager.enqueue_batch.assert_not_called()
        for update,expected in [({'status':'FAILED'},'RECOVERY_CACHE_INVALID'),
                                ({'script_status':'WAITING_APPROVAL'},'STATE_TRANSITION_REJECTED'),
                                ({'plan':'already-rendered.json'},'RECOVERY_CACHE_INVALID'),
                                ({'issues':['DIFFERENT_FAILURE']},'STATE_TRANSITION_REJECTED')]:
            with self.subTest(update=update),tempfile.TemporaryDirectory() as d:
                store,manager,ident,_=self.pre_tts_fixture(Path(d));store.item(ident,7,**update)
                with self.assertRaisesRegex(ValueError,expected):request(manager,ident,7,['07'])
                manager.enqueue_batch.assert_not_called()

    def test_exception_keeps_all_four_edges_and_default_still_rejects(self):
        import subprocess
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=fixture(root,(180,322));before=sha(source['path'])
            with self.assertRaisesRegex(ValueError,'SOURCE_FRAME_TOO_TALL'):geometry(source,180,320)
            layout=geometry(source,180,320,HEIGHT_POLICY)
            self.assertEqual(layout['foreground'],[1.,0,178,320])
            graph=filters(source,'0:v:0',180,320,'fit',HEIGHT_POLICY)
            out=root/'frame.png'
            subprocess.run(['ffmpeg','-v','error','-i',source['path'],'-filter_complex',graph,'-map','[fitout]','-frames:v','1','-y',str(out)],check=True)
            im=Image.open(out).convert('RGB')
            for xy,channel in [((3,160),0),((176,160),1),((90,3),0),((90,316),2)]:
                self.assertGreater(im.getpixel(xy)[channel],180)
            self.assertEqual(sha(source['path']),before)
            with self.assertRaisesRegex(ValueError,'INVALID_REQUEST'):snapshot({},dict(mode='Transport',source_frame_mode=HEIGHT_POLICY))
            self.assertEqual(snapshot({},dict(mode='Remix'))['source_frame_mode'],AUTO_POLICY)

    def test_height_exception_reaches_real_final_and_preview_render(self):
        import subprocess
        from PIL import Image
        from autocut.pipeline import make_plan
        from app.s3.headless_media import render_plan
        from app.s3.overlay_policy import lock_plan
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=fixture(root,(480,854));(root/'plans').mkdir()
            audio=dict(path=source['path'],sha256=source['sha256'],duration=2.)
            plan=make_plan(source,audio,{},dict(title_y=.17,subtitle_y=.78,override_reason='NONE'),'Um carinho especial',root,root/'final.mp4',dict(usage_scope='test',language='pt-BR'),task_overrides=dict(source_frame_mode=POLICY,title_max_lines=2),music_only=True,source_frame_exceptions={source['sha256']:HEIGHT_POLICY})
            self.assertEqual(plan['source']['source_frame_layout']['foreground'],[1.,0,1078,1920])
            for montage in [False,True]:
                current=copy.deepcopy(plan)
                if montage:
                    current.pop('source_ranges');current['montage_segments']=[dict(source,source_start=0.,source_end=2.,source_duration=2.,output_start=0.,output_end=2.,speed=1.)];bind(current);lock_plan(current)
                for preview in [False,True]:
                    name=str(montage)+'-'+str(preview);out=root/(name+'.mp4');render_plan(current,out,root/name,preflight=preview)
                    image=root/(name+'.png');subprocess.run(['ffmpeg','-v','error','-ss','1','-i',str(out),'-frames:v','1','-y',str(image)],check=True)
                    im=Image.open(image).convert('RGB');w,h=im.size
                    self.assertGreater(im.getpixel((round(w*.025),h//2))[0],180)
                    self.assertGreater(im.getpixel((round(w*.975),h//2))[1],180)
                    self.assertGreater(im.getpixel((w//2,5))[0],180)
                    self.assertGreater(im.getpixel((w//2,h-6))[2],180)
            self.assertEqual(sha(source['path']),source['sha256'])

    def test_only_exact_hash_and_montage_references_get_exception(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'tall').mkdir();(root/'wide').mkdir()
            tall=fixture(root/'tall',(180,322));wide=fixture(root/'wide')
            plan=dict(source=copy.deepcopy(wide),visual=dict(canvas=[1080,1920]),task_overrides=dict(source_frame_mode=POLICY),source_frame_exceptions={tall['sha256']:HEIGHT_POLICY},montage_segments=[copy.deepcopy(wide),copy.deepcopy(tall)])
            bind(plan)
            self.assertEqual(plan['source_frame_mode'],POLICY)
            self.assertEqual(plan['montage_segments'][0]['source_frame_mode'],POLICY)
            self.assertEqual(plan['montage_segments'][1]['source_frame_mode'],HEIGHT_POLICY)
            plan['source_frame_exceptions']={}
            with self.assertRaisesRegex(ValueError,'SOURCE_FRAME_TOO_TALL'):bind(plan)

    def test_recovery_retains_paid_audio_words_and_completed_peer_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=fixture(root,(180,322));audio=root/'audio.mp3';audio.write_bytes(b'already-paid')
            store=Store(root/'data');j,_=store.create(dict(request_id='frame-exception',product_name='Hat',mode='Transport'))
            ident=j['id'];store.update(ident,source_frame_mode=POLICY,batch_review={'phase':'RUNNING'})
            for rank in [1,2]:store.item(ident,rank,status='FAILED',issues=['SOURCE_FRAME_TOO_TALL'],source_snapshot=dict(source,rank=rank),analysis={'ok':True},script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,final_script='Original speech.',final_title='Original title.',audio_result=dict(path=str(audio),sha256=sha(audio),script_text='Original speech.',fingerprint='paid-request'))
            with patch('autocut.ownership.assert_final'):
                store.item(ident,10,status='PASS',output='completed.mp4',audio_result={'sha256':'kept'},final_script='Kept')
            peer=copy.deepcopy(store.get(ident)['items'][-1]);before=copy.deepcopy(store.get(ident)['items'][0]);queued=[]
            manager=MagicMock();manager.store=store;manager.lock=threading.RLock();manager.control.busy.return_value=False;manager.enqueue_batch.side_effect=lambda *a:queued.append(a)
            request(manager,ident,2,['01','02','10']);request(manager,ident,2,['01','02','10'])
            job=store.get(ident);self.assertEqual(job['source_frame_exceptions'],{source['sha256']:HEIGHT_POLICY});self.assertEqual(len(queued),1)
            self.assertEqual(queued[0][1],[('01','SCRIPT'),('02','SCRIPT')]);self.assertEqual(job['items'][-1],peer)
            for key in ['final_script','final_title','audio_result','script_status','title_status','content_confirmed']:self.assertEqual(job['items'][0][key],before[key])
            self.assertEqual(sha(audio),before['audio_result']['sha256'])

    def test_corrupt_audio_or_missing_real_confirmation_never_queues(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=fixture(root,(180,322));store=Store(root/'data');j,_=store.create(dict(request_id='reject',product_name='Hat',mode='Transport'));ident=j['id']
            store.update(ident,source_frame_mode=POLICY,batch_review={'phase':'RUNNING'})
            store.item(ident,2,status='FAILED',issues=['SOURCE_FRAME_TOO_TALL'],source_snapshot=source,analysis={'ok':True},script_status='WAITING_APPROVAL')
            m=MagicMock();m.store=store;m.lock=threading.RLock();m.control.busy.return_value=False
            with self.assertRaisesRegex(ValueError,'STATE_TRANSITION_REJECTED'):request(m,ident,2,['02'])
            store.item(ident,2,script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,final_script='Kept',audio_result={'path':source['path'],'sha256':'wrong','script_text':'Kept'})
            with self.assertRaisesRegex(ValueError,'RECOVERY_CACHE_INVALID'):request(m,ident,2,['02'])
            m.enqueue_batch.assert_not_called();self.assertNotIn('source_frame_exceptions',store.get(ident))


class AsrDraftRecoveryTests(unittest.TestCase):
    setUp=ReviewTests.setUp
    tearDown=ReviewTests.tearDown
    job=ReviewTests.job

    def prepare(self):
        self.store.item(self.id,1,copy={},title_candidates=[],final_script=None,script_draft={},audio_result=None,output=None,script_status=None)
        self.store.item(self.id,2,status='WAITING_SCRIPT_APPROVAL',script_draft={'id':'peer-draft'},final_script=None)
        self.work=self.store.home/'jobs'/self.id/'work';(self.work/'checkpoints').mkdir(parents=True,exist_ok=True)
        inputs=dict(source_sha256=self.media['sha256'],model_sha256='model',binary_sha256='binary',language='auto')
        self.failed=self.work/'checkpoints'/(fingerprint(['ASR',inputs])+'.json');write(self.failed,dict(state='REVIEW_REQUIRED',paid=False,error='STAGE_FAILED'))
        self.h=MagicMock();self.h.__enter__.return_value=self.h;self.h.dependency_hash.side_effect=lambda p:'model' if p.name.endswith('.bin') else 'binary'
        self.transcript=self.work/'transcript.json';write(self.transcript,dict(text='Look at this compact buckle.',language='en'))
        self.token=lambda:item_state(self.store,self.store.get(self.id),self.store.get(self.id)['items'][0])['action_token']

    def test_only_failed_unpaid_asr_retried_draft_still_needs_human(self):
        self.prepare();peer=copy.deepcopy(self.store.get(self.id)['items'][1]);f=future(dict(transcript=str(self.transcript)))
        with patch('autocut.source_asr_recovery.Harness',return_value=self.h),patch('autocut.source_asr_recovery.asr_job',return_value=f) as asr,patch('autocut.source_asr_recovery.analyze_sources',return_value=[dict(analysis=dict(ANALYSIS,clean_script='Look at this compact buckle.'),sheet='new-sheet.jpg')]) as analyze:
            result=recover(self.service,self.id,1,self.token());asr.assert_called_once();analyze.assert_called_once()
            with self.assertRaisesRegex(ValueError,'STATE_TRANSITION_REJECTED'):recover(self.service,self.id,1,self.token())
        item=self.store.get(self.id)['items'][0];self.assertTrue(result['candidates']);self.assertEqual(item['script_status'],'WAITING_APPROVAL');self.assertFalse(item.get('content_confirmed'));self.assertFalse(item.get('audio_result'));self.assertFalse(item.get('final_script'));self.assertEqual(self.q.enqueued,[]);self.assertEqual(self.store.get(self.id)['items'][1],peer)
        self.assertEqual(len(list((self.work/'recovery_archive').glob('*.json'))),1)

    def test_successful_or_paid_checkpoints_never_retry(self):
        self.prepare()
        for record in [dict(state='DONE',paid=False),dict(state='REVIEW_REQUIRED',paid=True,error='STAGE_FAILED')]:
            write(self.failed,record)
            with patch('autocut.source_asr_recovery.Harness',return_value=self.h),patch('autocut.source_asr_recovery.asr_job') as asr:
                with self.assertRaisesRegex(ValueError,'STATE_TRANSITION_REJECTED'):recover(self.service,self.id,1,self.token())
                asr.assert_not_called()

    def test_failure_archived_and_second_attempt_blocked(self):
        self.prepare()
        with patch('autocut.source_asr_recovery.Harness',return_value=self.h),patch('autocut.source_asr_recovery.asr_job',return_value=future({'error':'STAGE_FAILED'})) as asr:
            with self.assertRaisesRegex(ValueError,'SOURCE_ASR_RECOVERY_FAILED'):recover(self.service,self.id,1,self.token())
            with self.assertRaisesRegex(ValueError,'RECOVERY_ALREADY_REQUESTED'):recover(self.service,self.id,1,self.token())
            asr.assert_called_once()
        self.assertFalse(self.store.get(self.id)['items'][0].get('operation_inflight'))

    def test_approved_words_never_enter_asr_recovery(self):
        self.prepare();self.store.item(self.id,1,final_script=BR,script_status='USER_APPROVED')
        with patch('autocut.source_asr_recovery.asr_job') as asr:
            with self.assertRaises(ValueError):recover(self.service,self.id,1,self.token())
            asr.assert_not_called()


if __name__=='__main__':unittest.main()
