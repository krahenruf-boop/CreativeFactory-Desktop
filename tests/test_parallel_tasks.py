"""Real worker queues, isolated ledgers and tiny local media; no paid calls."""
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import autocut.context
from autocut import parallel_tasks as parallel
from autocut.pipeline import Manager
from autocut.storage import Store
from app.s3.headless_runtime import Harness, fingerprint, sha
from app.s3.headless_jobs import tts_job


class ParallelTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gates = patch.dict(parallel._GATES,
            {name: parallel.ResourceGate(limit) for name, limit in parallel.RESOURCE_LIMITS.items()})
        self.gates.start()
        self.addCleanup(self.gates.stop)

    def wait(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('worker condition timed out')
            time.sleep(.005)

    def test_four_products_fifth_queues_duplicate_pause_and_stop_are_isolated(self):
        store = Store(self.root / 'data')
        manager = Manager(store, None, None)
        ids = [store.create(dict(request_id='parallel-'+str(n), product_name='并行验证',
                                 market='Brazil', mode='Transport'))[0]['id'] for n in range(5)]
        started = {id: threading.Event() for id in ids}
        release = threading.Event()
        counts = {}
        calls = []
        source = self.root / 'original.mp4'
        source.write_bytes(b'original stays untouched')
        original = sha(source)

        def run(id):
            calls.append(id)
            store.update(id, state='RUNNING')
            started[id].set()
            while not release.is_set():
                manager.check(id)
                counts[id] = counts.get(id, 0) + 1
                time.sleep(.005)
            store.update(id, state='WAITING_SCRIPT_APPROVAL')

        manager.run = run
        try:
            for id in ids:
                manager.enqueue(id)
            for id in ids[:4]:
                self.assertTrue(started[id].wait(2))
            self.assertFalse(started[ids[4]].wait(.04))
            original_future = manager.active[ids[0]]
            manager.enqueue(ids[0])
            self.assertIs(manager.active[ids[0]], original_future)
            manager.control.request(ids[0], 'PAUSE')
            self.wait(lambda: store.get(ids[0])['state'] == 'PAUSED')
            parked_count = counts[ids[0]]
            other_count = counts[ids[1]]
            self.wait(lambda: counts[ids[1]] > other_count + 2)
            self.assertEqual(counts[ids[0]], parked_count)
            manager.control.resume(ids[0])
            self.wait(lambda: counts[ids[0]] > parked_count)
            self.assertIs(manager.active[ids[0]], original_future)
            manager.stop(ids[0])
            self.assertTrue(started[ids[4]].wait(2))
            self.wait(lambda: store.get(ids[0])['state'] == 'STOPPED')
            self.assertEqual(calls.count(ids[0]), 1)
            self.assertEqual(sha(source), original)
        finally:
            release.set()
            for id in ids:
                if manager.control.busy(id):
                    manager.stop(id)
            manager.queue.shutdown(wait=True)

    def test_preflight_final_and_direct_renderer_share_two_slots(self):
        release = threading.Event()
        entered = []
        mutex = threading.Lock()
        harnesses = [Harness(self.root / 'jobs' / str(n) / 'work', render_workers=2) for n in range(4)]

        def render(n):
            with parallel.resource_slot('render'):
                out = self.root / (str(n)+'.mp4')
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=yellow:s=64x96:r=30', '-t', '0.2', '-pix_fmt', 'yuv420p',
                    '-y', str(out)], check=True)
                with mutex:
                    entered.append(n)
                if not release.wait(3):
                    raise RuntimeError('FIXTURE_TIMEOUT')
                return dict(status='PASS', artifacts=[dict(path=str(out), sha256=sha(out))])

        futures = []
        try:
            for n in range(8):
                stage = 'LAYOUT_PREFLIGHT' if n % 2 else 'FINAL_RENDER'
                futures.append(harnesses[n//2].submit(stage, n, dict(n=n), lambda n=n: render(n)))
            self.wait(lambda: len(entered) == 2)
            self.wait(lambda: parallel.resource_status()['render']['waiting'] == 6)
            self.assertEqual(parallel.resource_status()['render']['active'], 2)
            release.set()
            self.assertTrue(all(f.result(4)['status'] == 'PASS' for f in futures))
            self.assertEqual(len(entered), 8)
            self.assertEqual(parallel.resource_status()['render']['peak'], 2)
            self.assertEqual(parallel.resource_status()['render']['active'], 0)
        finally:
            release.set()
            for harness in harnesses:
                harness.close()

    def audio(self):
        path = self.root / 'voice.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=44100', '-t', '0.4', '-c:a', 'libmp3lame',
            '-y', str(path)], check=True)
        return path.read_bytes()

    def voice(self, harness, factory, scope='account-fixture'):
        payload = dict(text='Oi', voice_id='fixture', model_id='fixture')
        review = dict(title_status='USER_APPROVED', script_status='CODEX_APPROVED',
                      tts_text_sha256=fingerprint('Oi'))
        return tts_job(harness, payload, scope=scope, provider_factory=factory,
                       review=review, execute=True)

    def test_identical_parallel_tts_uses_one_paid_request_and_copies_audio(self):
        audio = self.audio()
        called = threading.Event()
        release = threading.Event()
        calls = []
        class Provider:
            def tts(self, payload):
                calls.append(payload)
                called.set()
                if not release.wait(3):
                    raise RuntimeError('FIXTURE_TIMEOUT')
                return dict(audio=audio, alignment=dict(characters=list('Oi'),
                    character_start_times_seconds=[0,.1], character_end_times_seconds=[.1,.2]))
        with ExitStack() as stack:
            harnesses=[stack.enter_context(Harness(self.root/'jobs'/name/'work')) for name in ['a','b','c','d']]
            try:
                one = self.voice(harnesses[0], Provider)
                self.assertTrue(called.wait(2))
                rest=[self.voice(h, Provider) for h in harnesses[1:]]
                self.assertTrue(all(not f.done() for f in rest))
                release.set()
                results=[one.result(4)]+[f.result(4) for f in rest]
                first=results[0]
                self.assertEqual(len(calls), 1)
                self.assertTrue(all(r['technical_status']=='PASS' for r in results))
                self.assertTrue(all(r['new_requests']==0 and r['prior_cache_hit'] for r in results[1:]))
                self.assertEqual(len({r['path'] for r in results}),4)
                self.assertEqual(len({sha(r['path']) for r in results}),1)
                self.assertEqual(Path(first['path']).read_bytes(), audio)
            finally:
                release.set()

    def test_uncertain_paid_attempt_does_not_call_provider_in_second_job(self):
        calls = []
        class Provider:
            def tts(self, payload):
                calls.append(payload)
                raise RuntimeError('BILLING_UNCERTAIN')
        with Harness(self.root/'jobs/a/work') as a, Harness(self.root/'jobs/b/work') as b:
            self.assertEqual(self.voice(a, Provider).result(3)['error'], 'BILLING_UNCERTAIN')
            self.assertEqual(self.voice(b, Provider).result(3)['error'],
                             'EXISTING_TTS_ATTEMPT_REQUIRES_RECONCILIATION')
            self.assertEqual(len(calls), 1)

    def test_moved_cached_audio_never_triggers_another_paid_request(self):
        audio = self.audio()
        calls = []
        class Provider:
            def tts(self, payload):
                calls.append(payload)
                return dict(audio=audio, alignment=dict(characters=list('Oi'),
                    character_start_times_seconds=[0,.1], character_end_times_seconds=[.1,.2]))
        with Harness(self.root/'jobs/a/work') as a, Harness(self.root/'jobs/b/work') as b:
            first = self.voice(a, Provider).result(3)
            Path(first['path']).rename(self.root/'retained.mp3')
            second = self.voice(b, Provider).result(3)
            self.assertEqual(second['error'], 'EXISTING_TTS_ARTIFACT_INVALID')
            self.assertEqual(len(calls), 1)
            self.assertEqual((self.root/'retained.mp3').read_bytes(), audio)

    def test_different_account_scopes_do_not_share_paid_audio(self):
        audio = self.audio()
        calls = []
        class Provider:
            def tts(self, payload):
                calls.append(payload)
                return dict(audio=audio, alignment=dict(characters=list('Oi'),
                    character_start_times_seconds=[0,.1], character_end_times_seconds=[.1,.2]))
        with Harness(self.root/'jobs/a/work') as a, Harness(self.root/'jobs/b/work') as b:
            one = self.voice(a, Provider, 'account-one')
            two = self.voice(b, Provider, 'account-two')
            first, second = one.result(3), two.result(3)
            self.assertNotEqual(first['fingerprint'], second['fingerprint'])
            self.assertEqual(first['technical_status'], 'PASS')
            self.assertEqual(second['technical_status'], 'PASS')
            self.assertEqual(len(calls), 2)

    def test_shared_limit_releases_after_failure_and_rate_limit_is_global(self):
        def fail():
            raise RuntimeError('FIXTURE_FAILED')
        with Harness(self.root/'a') as a, Harness(self.root/'b') as b:
            failed = a.submit('FINAL_RENDER', 1, dict(n=1), fail)
            done = b.submit('FINAL_RENDER', 2, dict(n=2), lambda: dict(status='PASS'))
            self.assertEqual(failed.result(3)['error'], 'FIXTURE_FAILED')
            self.assertEqual(done.result(3)['status'], 'PASS')
            self.assertEqual(parallel.resource_status()['render']['active'], 0)
        parallel.tts_rate_limited()
        self.assertEqual(parallel.resource_status()['tts']['limit'], 2)

    def test_compatibility_adapter_is_idempotent(self):
        from parallel_tasks_compatibility import apply
        engine = self.root/'engine'
        paths = [Path('app/s3')/n for n in ['headless_runtime.py','headless_media.py','headless_jobs.py']]
        for path in paths:
            dest = engine/path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path('staging/engine')/path, dest)
        before = {str(p):sha(engine/p) for p in paths}
        apply(engine)
        apply(engine)
        self.assertEqual(before, {str(p):sha(engine/p) for p in paths})

    def test_runtime_diagnostics_report_loaded_limits_without_mutating_jobs(self):
        from fastapi.testclient import TestClient
        from autocut.server import create_app
        store = Store(self.root/'data')
        manager = Manager(store, None, None)
        before = store.jobs()
        try:
            with TestClient(create_app(store, None, None, manager, 'local-fixture',
                                     recover_on_startup=False), base_url='http://127.0.0.1:9999',
                            headers={'Authorization':'Bearer local-fixture'}) as client:
                result = client.get('/runtime/concurrency')
                self.assertEqual(result.status_code, 200)
                data = result.json()
                self.assertEqual(data['max_product_jobs'], 4)
                self.assertEqual(data['policy'], 'PRODUCT_PARALLEL_V2')
                self.assertEqual(data['resources']['render']['limit'], 2)
                self.assertEqual(data['running_jobs'], 0)
                self.assertEqual(data['feishu_transfer_limit'], 1)
                self.assertEqual(store.jobs(), before)
        finally:
            manager.queue.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
