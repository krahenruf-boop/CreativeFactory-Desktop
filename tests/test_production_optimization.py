"""Production prevention and evidence against isolated files/SQLite, no paid APIs."""
import copy
import inspect
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import autocut.context
import test_batch_workspace as fixtures
from autocut.production_preflight import estimate, duration_view, before_tts, require_title, title_check
from autocut.source_library import POLICY, usable_intervals, trim_clips, inspect_source, adapt
from autocut.runtime_insights import diagnostic, metrics, health, record, state_seconds
from autocut.storage import Store
from autocut.review_media import base_clips
from autocut.supplement_recovery import context as supplement_context
from autocut.feishu_tasks import cards
from app.s3.headless_runtime import Harness, read, sha, fingerprint
from app.s2.tts_text import tts_request_payload


def payload(n=30, voice='fixture'):
    text=' '.join(['produto']*n)
    return dict(text=text,tts_text=text,voice_id=voice,model_id='model',language='pt',
                settings={'speed':1.},output_format='mp3_44100_128',previous_text=None,next_text=None)


def calibration():
    return [dict(fingerprint='request-'+str(n),scope='account',technical_status='PASS',
        payload=payload(n),duration=n*.5) for n in range(20,45)]


def fixture_job(duration=5):
    source=dict(path='/fixture/source.mp4',sha256='a',duration=duration,rank=1,status='VALID',width=1080,height=1920)
    item=dict(rank='01',source_snapshot=source,analysis={'product_description':'same product'},decision={},
              copy={'localized_script':{'text':payload()['text']}},status='WAITING_SUPPLEMENT_MEDIA')
    job=dict(id='fixture',input_path='/fixture',production_preflight_policy=POLICY,mode='Transport',items=[item])
    return job,item


class SourceLibraryTests(unittest.TestCase):
    def test_only_short_edge_black_is_removed(self):
        self.assertEqual(usable_intervals(10,[[0,.1],[3,3.2],[9.87,10]]),[[.1,9.866666666666667]])
        self.assertEqual(usable_intervals(10,[[0,.6],[9.4,10]]),[[0.,10]])
        self.assertEqual(usable_intervals(.5,[[0,.4]]),[[0.,.5]])

    def test_transport_and_remix_use_identical_usable_intervals(self):
        h=dict(status='READY',usable_intervals=[[.1,9.8]])
        source=dict(path='same',sha256='sha',duration=10,rank=1,source_health=h)
        first=base_clips(source,{},{});remix=dict(source,remix_clips=[dict(first[0],source_start=0,source_end=10)])
        self.assertEqual(base_clips(remix,{},{}),first)
        self.assertAlmostEqual(adapt(source,first)['duration'],9.7)
        self.assertEqual(first[0]['source_start'],.1)
        self.assertEqual(source['duration'],10)

    def test_real_black_tail_and_cache_preserve_source(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=90x160:r=30:d=1',
                '-f','lavfi','-i','color=black:s=90x160:r=30:d=0.133333',
                '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0','-c:v','libx264','-pix_fmt','yuv420p','-y',str(path)],check=True)
            source=dict(path=str(path),sha256=sha(path),width=90,height=160,duration=34/30,rank=1)
            check=inspect_source(root,source)
            self.assertEqual(check['status'],'READY');self.assertLess(check['effective_duration'],source['duration'])
            self.assertEqual(sha(path),source['sha256'])
            from autocut.pipeline import make_plan
            from app.s3.headless_media import render_plan
            audio=root/'voice.wav'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1','-y',str(audio)],check=True)
            indexed=dict(source,source_health=check);clips=base_clips(indexed,{},{});adapted=adapt(indexed,clips)
            (root/'plans').mkdir()
            plan=make_plan(adapted,dict(path=str(audio),sha256=sha(audio),duration=1.),{},
                dict(title_y=.17,subtitle_y=.78,override_reason='NONE'),'Um carinho especial',root,root/'final.mp4',
                dict(usage_scope='test',language='pt-BR'),task_overrides=dict(source_frame_mode='WIDTH_FIT_BLUR_V1',title_max_lines=2),music_only=True)
            self.assertLessEqual(plan['montage_segments'][0]['source_end'],1.)
            for preview in (True,False):
                output=root/('preview.mp4' if preview else 'final.mp4')
                render_plan(plan,output,root/('preflight' if preview else 'export'),preflight=preview)
                subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(output),'-f','null','-'],check=True)
            self.assertEqual(sha(path),source['sha256'])
            with patch('autocut.source_library.subprocess.run',side_effect=AssertionError('must reuse')):
                self.assertEqual(inspect_source(root,source),check)
            source['sha256']='changed'
            with self.assertRaisesRegex(ValueError,'SOURCE_HASH_CHANGED'):inspect_source(root,source)


class DurationPreflightTests(unittest.TestCase):
    def test_copy_policy_preserves_manuscript_and_old_contexts(self):
        from autocut.workbench import approval_contexts
        source=[dict(video_context='facts',source_script='unchanged source',clean_script='unchanged manuscript')]
        before=copy.deepcopy(source);j,x=fixture_job()
        output=approval_contexts(source,j)
        self.assertEqual(source,before);self.assertEqual(output[0]['clean_script'],source[0]['clean_script'])
        self.assertIn('PRESERVE_SCRIPT_FIRST_V1',output[0]['video_context'])
        self.assertEqual(approval_contexts(source,{}),before)

    def test_calibration_is_scoped_and_deduplicated(self):
        sample=calibration();r=estimate(payload(),'account',sample)
        self.assertTrue(r['reliable']);self.assertEqual(r['lower'],15)
        for modified in [payload(30,'other'),dict(payload(),settings={'speed':1.2}),dict(payload(),language='es')]:
            self.assertFalse(estimate(modified,'account',sample)['reliable'])
        self.assertFalse(estimate(payload(),'other-account',sample)['reliable'])
        self.assertFalse(estimate(payload(),'account',[sample[0]]*30)['reliable'])
        self.assertFalse(estimate(payload(100),'account',sample)['reliable'])

    def test_exact_valid_audio_beats_estimate_and_invalid_cache_not_repaid(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'existing.mp3';path.write_bytes(b'existing')
            p=payload();key=fingerprint(dict(provider='elevenlabs',scope='account',kind='tts',payload=tts_request_payload(p),schema='s2-v1',generation=None))
            receipt=dict(fingerprint=key,scope='account',path=str(path),sha256=sha(path),duration=42,technical_status='PASS')
            self.assertEqual(estimate(p,'account',[receipt])['lower'],42)
            path.unlink();self.assertEqual(estimate(p,'account',[receipt])['basis'],'CACHE_REQUIRES_REVIEW')

    def test_explicit_deficit_waits_before_paid_call_unknown_does_not(self):
        j,x=fixture_job();original=copy.deepcopy(x);store=MagicMock()
        with patch('autocut.source_frame.geometry',return_value=None):
            self.assertFalse(before_tts(store,j,x,payload(),'account',calibration(),base_clips(x['source_snapshot'],{},{})))
            self.assertEqual(store.item.call_args.kwargs['status'],'WAITING_SUPPLEMENT_MEDIA')
            store.reset_mock()
            self.assertTrue(before_tts(store,j,x,payload(),'account',[],base_clips(x['source_snapshot'],{},{})))
        self.assertEqual(x,original)

    def test_allowed_sibling_budget_and_cap_are_unchanged(self):
        j,x=fixture_job(8);donor=copy.deepcopy(x);donor['rank']='02';donor['source_snapshot'].update(rank=2,sha256='b',duration=10)
        j['items'].append(donor)
        r=duration_view(j,x,payload(),'account',calibration());self.assertEqual(r['status'],'WARNING')
        x['source_snapshot']['duration']=3
        self.assertEqual(duration_view(j,x,payload(),'account',calibration())['status'],'WAITING_MEDIA')

    def test_prepaid_wait_can_use_existing_supplement_workflow(self):
        j,x=fixture_job();x['production_preflight']=dict(status='WAITING_MEDIA',duration_upper=18)
        result=supplement_context(j,x,x['source_snapshot'],{},[])
        self.assertTrue(result['duration_is_estimate']);self.assertEqual(result['tts_duration'],18)
        old=dict(j);old.pop('production_preflight_policy')
        with self.assertRaisesRegex(ValueError,'SCRIPT_CONFIRMATION_REQUIRED'):supplement_context(old,x,x['source_snapshot'],{},[])

    def test_future_default_does_not_apply_to_historical_job(self):
        j,x=fixture_job();j.pop('production_preflight_policy')
        store=MagicMock();self.assertTrue(before_tts(store,j,x,payload(),'account',calibration(),[]));store.item.assert_not_called()


class TitlePreflightTests(unittest.TestCase):
    def test_real_two_line_fit_and_unsupported_title(self):
        j,x=fixture_job()
        r=title_check(j,x,'Como tapar um buraco no drywall sem chamar ninguém')
        self.assertEqual(r['status'],'PASS');self.assertEqual(len(r['lines']),2);self.assertGreaterEqual(r['font_size'],48)
        with self.assertRaisesRegex(ValueError,'TITLE_SHORTEN_REQUIRED'):
            require_title(j,x,'Aposto que 99% das pessoas não sabe como tapar um buraco no drywall sem chamar ninguém.')
        self.assertEqual(title_check(j,x,'Produto incrível ♀')['status'],'PASS')


class ConfirmationIntegrationTests(unittest.TestCase):
    setUp=fixtures.BatchTests.setUp;tearDown=fixtures.BatchTests.tearDown
    raw=fixtures.BatchTests.raw;state=fixtures.BatchTests.state;post=fixtures.BatchTests.post
    public=fixtures.BatchTests.public;view=fixtures.BatchTests.view;payload=fixtures.BatchTests.payload
    submit=fixtures.BatchTests.submit;scripts=fixtures.BatchTests.scripts;rows=fixtures.BatchTests.rows

    def prepare(self):
        self.store.update(self.id,production_preflight_policy=POLICY)
        self.scripts()
        for item in self.rows():
            titles=copy.deepcopy(item['title_candidates'])
            for t in titles:t['text']='Produto incrível para você'
            titles[0]['text']=' '.join(['Olha este produto para você']*20)
            response=copy.deepcopy(item['copy']);response['titles']=titles
            self.store.item(self.id,item['rank'],title_candidates=titles,copy=response)

    def test_batch_selected_invalid_rejected_atomically_valid_alternative_accepted(self):
        self.prepare();before=self.rows();view=self.view()
        self.assertTrue(view['all_titles_ready'])
        self.assertEqual(view['items'][0]['title_candidates'][0]['preflight']['status'],'BLOCKED')
        result=self.submit('titles');self.assertEqual(result.status_code,400);self.assertEqual(result.json()['error'],'TITLE_SHORTEN_REQUIRED')
        self.assertEqual(self.rows(),before);self.assertEqual(self.queue.enqueued,[])
        request=self.payload('titles');request['selections']={r['rank']:2 for r in self.rows()}
        self.assertEqual(self.submit('titles',request).status_code,200)
        queued=list(self.queue.enqueued)
        self.assertEqual(self.submit('titles',request).status_code,200);self.assertEqual(self.queue.enqueued,queued)

    def test_feishu_card_explains_fit_and_disables_only_invalid_option(self):
        self.prepare();view=self.view();task=dict(id='CF-fixture',product_name='产品',selections={},page=0)
        value=cards.review(task,view);buttons=[]
        for element in value['elements']:
            if element.get('tag')=='action':buttons.extend(element['actions'])
        titles=[b for b in buttons if b.get('value',{}).get('action')=='select_title']
        self.assertTrue(titles[0]['disabled']);self.assertFalse(titles[1]['disabled'])
        self.assertIn('排版',json.dumps(value,ensure_ascii=False))

    def test_single_approval_has_identical_guard(self):
        self.store.update(self.id,production_preflight_policy=POLICY,batch_review_required=False)
        item=self.raw();titles=copy.deepcopy(item['copy']['titles']);titles[0]['text']=' '.join(['Olha este produto para você']*20)
        self.store.item(self.id,'01',title_candidates=titles,content_titles=titles)
        before=self.rows();response=self.post('content/approve',dict(title_id=1))
        self.assertEqual(response.status_code,400);self.assertEqual(response.json()['error'],'TITLE_SHORTEN_REQUIRED')
        self.assertEqual(self.rows(),before)


class OperationalEvidenceTests(unittest.TestCase):
    def test_monitor_discovers_fresh_state_without_resuming_or_leaking_auth(self):
        import importlib.util,io
        spec=importlib.util.spec_from_file_location('monitor_check',Path('scripts/creative_factory_monitor_check.py').resolve())
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);(home/'runtime').mkdir();(home/'runtime/backend.json').write_text(json.dumps(dict(port=52857,pid=123,token='private-fixture-token')))
            calls=[]
            values={'/jobs':[dict(id='new',state='WAITING_TITLE_APPROVAL')],'/runtime/active':dict(count=0),
                '/runtime/concurrency':{},'/integrations/feishu-tasks':dict(tasks=[]),
                '/jobs/new/content-review':dict(id='new',state='WAITING_TITLE_APPROVAL',items=[dict(rank='01',status='WAITING_TITLE_APPROVAL',action_token='private-action')])}
            def open_request(request,timeout):
                from urllib.parse import urlparse
                path=urlparse(request.full_url).path;calls.append((request.get_method(),path))
                return io.BytesIO(json.dumps(values[path]).encode())
            opener=MagicMock();opener.open.side_effect=open_request
            with patch.object(module.urllib.request,'build_opener',return_value=opener):
                result=module.check(home,'heartbeat');module.check(home,'heartbeat')
            self.assertTrue(all(method=='GET' for method,path in calls));self.assertNotIn(('GET','/jobs/new'),calls)
            saved=(home/'runtime/monitor-observations.json').read_text();self.assertNotIn('private',saved)
            self.assertEqual(len(json.loads(saved)['events']),2);self.assertEqual(result['pending'][0]['id'],'new')

    def test_errors_are_categorical_and_exit_code_is_retained(self):
        error=subprocess.CalledProcessError(7,['ffmpeg','private-token'],stderr='private-token')
        self.assertEqual(diagnostic(error),dict(code='STAGE_FAILED',category='CalledProcessError',exit_code=7))
        self.assertNotIn('private',json.dumps(diagnostic(error)))

    def test_actual_harness_failure_cache_and_paid_guard_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            with Harness(Path(d)) as h:
                def failed():raise subprocess.CalledProcessError(9,['private'],stderr='secret')
                result=h.submit('ASR','01',dict(source='input'),failed).result()
                self.assertEqual(result['error'],'STAGE_FAILED')
                h.submit('TTS','paid',{},lambda:dict(status='REVIEW_REQUIRED',error='BILLING_UNCERTAIN'),paid=True).result()
                callback=MagicMock(return_value={})
                h.submit('TTS','paid',{},callback,paid=True).result();callback.assert_not_called()
                callback=MagicMock(return_value=dict(status='PASS'))
                h.submit('ASR','02',dict(source='other'),callback).result()
                h.submit('ASR','02',dict(source='other'),callback).result();callback.assert_called_once()
            events=read(Path(d)/'operational-events.json')['events']
            error=next(e for e in events if e.get('kind')=='ATTEMPT' and e['item']=='01')
            self.assertEqual(error['diagnostic']['exit_code'],9)
            self.assertNotIn('secret',json.dumps(events));self.assertTrue(any(e.get('cache_hit') for e in events))

    def test_metrics_unknown_history_and_waits_not_failures(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));old,_=store.create(dict(request_id='old',product_name='old',market='Brazil'))
            self.assertIsNone(metrics(store)['first_pass_rate'])
            job,_=store.create(dict(request_id='new',product_name='new',market='Brazil',production_preflight_policy=POLICY))
            store.item(job['id'],1,status='WAITING_SCRIPT_APPROVAL');store.item(job['id'],1,status='WAITING_TITLE_APPROVAL')
            store.item(job['id'],1,status='VOICE')
            with store.connect() as db:
                row=json.loads(db.execute('select data from job_items where job_id=?',(job['id'],)).fetchone()[0]);row['status']='PASS'
                db.execute('update job_items set data=? where job_id=?',(json.dumps(row),job['id']))
            record(store.home/'jobs'/job['id']/'work',dict(kind='STATE',component='01',status='PASS'))
            m=metrics(store);self.assertEqual(m['first_pass_rate'],1.);self.assertEqual(m['exception_interventions'],0)
            self.assertEqual(len(m['jobs']),1);self.assertIsNone(m['cache_hit_rate'])

    def test_health_and_metrics_are_readonly_and_do_not_create_store(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));bridge=MagicMock();bridge.status.return_value=dict(connected=True,accepting=True,quark_health={'ready':True})
            before=store.jobs()
            with patch('autocut.storage.Store.__init__',side_effect=AssertionError('must not create Store')):
                for _ in range(2):health(store,None,bridge,{});metrics(store)
            self.assertEqual(store.jobs(),before)


if __name__=='__main__':unittest.main()
