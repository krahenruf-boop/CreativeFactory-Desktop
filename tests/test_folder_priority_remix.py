"""Per-job folder opening order, full coverage, and native approval gates."""
import copy
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import autocut.context
from autocut.custom_remix import validate, snapshot, selection, contexts, priority_paths, source_priorities
from autocut.remix import remix_items, apply_montage


class FolderPriorityRemixTests(unittest.TestCase):
    def spec(self):
        return dict(mode='Remix', market='Brazil', output_count=10, custom_remix=dict(
            coverage='ALL_SOURCES_BALANCED', source_folder_priority=['1', '2', '3'],
            script_groups=[dict(ranks=[n], angle='VERIFIED_PRODUCT') for n in range(1, 11)]))

    def sources(self):
        rows = [dict(rank=n, duration=8. + (n * 7 % 35), path=f'{n}.mp4', sha256=str(n), filename=f'{n}.mp4')
                for n in range(1, 29)]
        analyses = {s['rank']: dict(clean_script='This visor has an open top.', context='Open-top sun visor worn with a ponytail.',
                    selling_points=['open top'], allowed_claims=['open top'], overlays_need_review=False,
                    existing_arrow=False, source_language='en', voice_quality='GOOD') for s in rows}
        priorities = {n: 0 if n <= 9 else 1 if n <= 18 else 2 for n in range(1, 29)}
        return rows, analyses, priorities

    def test_28_sources_cover_ten_outputs_and_best_openers_are_distributed_first(self):
        sources, analyses, priorities = self.sources()
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for source in sources:
            path = Path(directory.name) / source['filename']; path.write_bytes(str(source['rank']).encode())
            source.update(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        before = copy.deepcopy(sources)
        chosen = selection(sources, analyses, 10, priorities=priorities)
        items, mapped = remix_items(chosen, sources, analyses, 10, True)
        self.assertEqual(sources, before)
        self.assertEqual({c['sha256'] for item in items for c in item['remix_clips']}, {s['sha256'] for s in sources})
        self.assertGreaterEqual(sum(priorities[item['remix_clips'][0]['source_rank']] == 0 for item in items), 9)
        for item in items:
            groups = [priorities[c['source_rank']] for c in item['remix_clips']]
            self.assertEqual(groups, sorted(groups))
            plan = apply_montage(dict(target_duration=item['duration'], video_speed=1.), item)
            self.assertEqual(plan['montage_segments'][0]['source_rank'], item['remix_clips'][0]['source_rank'])
        self.assertAlmostEqual(sum(x['duration'] for x in items), sum(s['duration'] for s in sources))
        for source in sources:
            spans = sorted((c['start'], c['end']) for out in chosen['outputs'] for c in out['clips'] if c['source_rank'] == source['rank'])
            self.assertEqual(spans[0][0], 0.)
            self.assertEqual(spans[-1][1], source['duration'])
            self.assertTrue(all(a[1] == b[0] for a, b in zip(spans, spans[1:])))

    def test_missing_priority_cannot_silently_skip_a_source(self):
        sources, analyses, priorities = self.sources(); priorities.pop(28)
        with self.assertRaisesRegex(ValueError, 'CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED'):
            selection(sources, analyses, 10, priorities=priorities)

    def test_scan_reads_only_selected_subfolders_and_keeps_original_file_hashes(self):
        from app.s3.headless_runtime import Harness
        from scripts.headless_transport import scan
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); inputs = root/'input'; inputs.mkdir()
            paths = []
            for name in ['1', '2', '3']:
                (inputs/name).mkdir(); paths.append(inputs/name/'same.mp4')
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=72x128:d=1',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(paths[0])], check=True, capture_output=True)
            for path in paths[1:]: path.write_bytes(paths[0].read_bytes())
            digest = hashlib.sha256(paths[0].read_bytes()).hexdigest()
            with Harness(root/'work') as harness:
                rows = scan(harness, inputs, paths=priority_paths(inputs, ['1', '2', '3']))
            self.assertEqual([r['rank'] for r in rows], [1, 2, 3])
            self.assertTrue(all(r['status'] == 'VALID' and r['sha256'] == digest for r in rows))
            self.assertEqual([Path(r['path']).parent.name for r in rows], ['1', '2', '3'])
            self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p in paths))

    def test_generic_contexts_use_verified_product_facts_without_gift_or_offer(self):
        sources, analyses, priorities = self.sources()
        items, mapped = remix_items(selection(sources, analyses, 10, priorities=priorities), sources, analyses, 10, True)
        job = self.spec(); job['custom_remix'] = validate(job)
        rows, mapping, _ = contexts(job, items, mapped, dict(product_name='高马尾遮阳帽'), 'pt-BR', '')
        self.assertEqual(len(rows), 10); self.assertEqual(len(set(mapping.values())), 10)
        for row in rows:
            self.assertIn('pt-BR', row['clean_script'])
            self.assertEqual(row['supported_selling_points'], ['open top'])
            self.assertNotIn('Romantic', row['clean_script'])
            self.assertNotIn('discount', row['allowed_claims'])
        saved = snapshot(job)
        self.assertEqual(saved['resolved_overrides_json']['values']['copy_style'], 'natural')
        self.assertTrue(any('1 → 2 → 3' in text for text in saved['instruction_summary']))

    def test_folder_selection_is_explicit_and_cannot_follow_symlinks_or_other_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ['1', '2', '3', 'outputs']:
                (root / name).mkdir(); (root / name / 'same.mp4').write_bytes(name.encode())
            paths = priority_paths(root, ['1', '2', '3'])
            self.assertEqual([p.parent.name for p in paths], ['1', '2', '3'])
            sources = [dict(rank=n, path=str(path)) for n, path in enumerate(paths, 1)]
            self.assertEqual(source_priorities(sources, root, ['1', '2', '3']), {1: 0, 2: 1, 3: 2})
            with self.assertRaisesRegex(ValueError, 'CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED'):
                source_priorities([dict(rank=1, path=str(root/'outputs/same.mp4'))], root, ['1', '2', '3'])
            (root / 'linked').symlink_to(root / '1', target_is_directory=True)
            with self.assertRaises(ValueError): priority_paths(root, ['linked'])

    def test_invalid_folder_names_or_mixed_copy_policy_are_rejected(self):
        for names in [['1', '1'], ['../outside'], ['1/sub'], ['1\\sub'], ['.hidden']]:
            bad = self.spec(); bad['custom_remix']['source_folder_priority'] = names
            with self.assertRaises(ValueError): validate(bad)
        bad = self.spec(); bad['custom_remix']['script_groups'][0]['angle'] = 'LOVE_GIFT_SYMBOL'
        with self.assertRaises(ValueError): validate(bad)

    def test_submission_freezes_folder_rules_and_real_confirmation_barriers(self):
        from fastapi.testclient import TestClient
        from autocut.storage import Store
        from autocut.server import create_app
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); store = Store(root/'isolated-data')
            class Manager:
                def __init__(self): self.started = []
                def enqueue(self, ident): self.started.append(ident)
            manager = Manager()
            client = TestClient(create_app(store, None, None, manager, 'isolated-fixture', recover_on_startup=False),
                                base_url='http://127.0.0.1:9000', headers={'Authorization': 'Bearer isolated-fixture'})
            client.post('/native/grant', json={'path': str(root)})
            spec = dict(self.spec(), request_id='folder-priority-fixture', input_path=str(root),
                        product_name='高马尾遮阳帽', product_profile_id='general', watermark_mode='handled')
            result = client.post('/jobs', json=spec)
            self.assertEqual(result.status_code, 200, result.text)
            job = store.get(result.json()['id'])
            self.assertEqual(job['custom_remix']['source_folder_priority'], ['1', '2', '3'])
            self.assertTrue(job['content_confirmation_required']); self.assertTrue(job['batch_review_required'])
            self.assertFalse(job.get('scripts_approved')); self.assertFalse(job.get('titles_approved'))
            self.assertEqual(client.post('/jobs', json=spec).json()['id'], job['id'])
            self.assertEqual(manager.started, [job['id']])


if __name__ == '__main__': unittest.main()
