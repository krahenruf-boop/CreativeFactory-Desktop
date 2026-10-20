"""Coverage and title-policy regression checks with isolated sources only."""
import copy
import tempfile
import unittest
import wave
from pathlib import Path
import autocut.context
from autocut.custom_remix import validate, selection, contexts
from autocut.remix import remix_items
from autocut.title_layout import apply
from app.s3.visual_preset import build_visual_plan, validate_visual_plan, text_layer
from app.s3.headless_runtime import sha
from app.s3.headless_media import validate_montage


class CustomRemixTests(unittest.TestCase):
    def spec(self):
        return dict(mode='Remix', market='Brazil', output_count=10, custom_remix=dict(coverage='ALL_SOURCES_BALANCED',
            script_groups=[dict(ranks=[1,2,3], angle='LOVE_GIFT_DAILY_COMPANION'), dict(ranks=[4,5,6], angle='LOVE_GIFT_SYMBOL')],
            music_only_ranks=[7,8,9,10], music_paths={f'{n:02}':'local.m4a' for n in range(7,11)}, title_lines=2))

    def test_partition_rejects_missing_duplicate_or_spoken_music_overlap(self):
        self.assertIsNotNone(validate(self.spec()))
        for ranks in [[7,8,9], [6,7,8,9,10], [7,8,9,9]]:
            s=self.spec();s['custom_remix']['music_only_ranks']=ranks
            with self.assertRaisesRegex(ValueError,'CUSTOM_REMIX_INVALID'):validate(s)
        s=self.spec();s['custom_remix']['music_paths'].pop('10')
        with self.assertRaisesRegex(ValueError,'CUSTOM_REMIX_MUSIC_REQUIRED'):validate(s)

    def test_submission_snapshots_music_and_preserves_human_barrier(self):
        from fastapi.testclient import TestClient
        from autocut.storage import Store
        from autocut.server import create_app
        from autocut.task_instructions import effective
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);music=root/'song.wav'
            with wave.open(str(music),'wb') as w:
                w.setparams((1,2,8000,0,'NONE','not compressed'));w.writeframes(b'\0\0'*16000)
            class Manager:
                def __init__(self):self.started=[]
                def enqueue(self,id):self.started.append(id)
            store=Store(root/'isolated-store');manager=Manager()
            client=TestClient(create_app(store,None,None,manager,'test-custom-local'),base_url='http://127.0.0.1:9000',headers={'Authorization':'Bearer test-custom-local'})
            for path in [root,music]:client.post('/native/grant',json=dict(path=str(path)))
            spec=self.spec();spec.update(request_id='custom-sunflower-isolated',input_path=str(root),product_profile_id='general',product_name='向日葵摆件',title_approval_mode='manual')
            spec['custom_remix']['music_paths']={f'{n:02}':str(music) for n in range(7,11)}
            result=client.post('/jobs',json=spec);self.assertEqual(result.status_code,200,result.text)
            j=store.get(result.json()['id']);self.assertTrue(j['content_confirmation_required']);self.assertFalse(j.get('scripts_approved'))
            self.assertEqual(effective(j,1)['title_max_lines'],2)
            self.assertEqual(len(j['custom_remix_music']),4);self.assertTrue(all(sha(m['path'])==sha(music) for m in j['custom_remix_music'].values()))
            self.assertEqual(client.post('/jobs',json=spec).json()['id'],j['id']);self.assertEqual(manager.started,[j['id']])

    def test_all_31_sources_used_balanced_with_exactly_two_manuscripts(self):
        sources=[dict(rank=n,duration=8+(n%9),path=f'{n}.mp4',sha256=str(n),filename=f'{n}.mp4') for n in range(1,32)]
        analysis={s['rank']:dict(clean_script='Original',context='Sunflower ornament in car and home',selling_points=['sunflower design'],allowed_claims=['sunflower design'],overlays_need_review=False,existing_arrow=False,source_language='zh',voice_quality='GOOD') for s in sources}
        chosen=selection(sources,analysis,10);items,mapped=remix_items(chosen,sources,analysis,10,selling_points_primary=True)
        clips=[c for x in items for c in x['remix_clips']]
        self.assertEqual(sorted(c['source_rank'] for c in clips),list(range(1,32)))
        self.assertAlmostEqual(sum(x['duration'] for x in items),sum(x['duration'] for x in sources))
        self.assertLessEqual(max(x['duration'] for x in items)-min(x['duration'] for x in items),max(x['duration'] for x in sources))
        job=self.spec();job['custom_remix']=validate(job)
        ctx,mapping,groups=contexts(job,items,mapped,dict(product_name='Sunflower ornament'),'pt-BR','')
        self.assertEqual(len(ctx),2);self.assertEqual(len(set(mapping[r] for r in [1,2,3])),1)
        self.assertEqual(len(set(mapping[r] for r in [4,5,6])),1);self.assertNotEqual(mapping[1],mapping[4])
        self.assertEqual(set(mapping),set(range(1,11)))
        with self.assertRaisesRegex(ValueError,'CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED'):selection(sources,{k:v for k,v in analysis.items() if k!=31},10)

    def test_two_line_title_shrinks_without_rewriting_and_renders(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'asset';p.write_bytes(b'fixture')
            asset=dict(id='fixture',file_path=str(p),sha256=sha(p))
            title='Um pequeno girassol para lembrar todos os dias do amor que vocês compartilham'
            for canvas in [(1080,1920),(720,1280)]:
                visual=build_visual_plan(36,title,[],asset=asset);visual['canvas']=list(canvas)
                fit=apply(visual,title,max_lines=2)
                self.assertEqual(len(fit['lines']),2);self.assertEqual(' '.join(fit['lines']),title)
                self.assertGreaterEqual(fit['reference_font_size'],48)
                self.assertEqual(visual['title']['end'],5);self.assertEqual(validate_visual_plan(visual),[])
                layer=text_layer(visual['title']['text'],visual['title'],visual['canvas'],Path(d)/f'{canvas[0]}.png')
                self.assertEqual(len(layer['lines']),2)
            oversized='X'*160
            with self.assertRaisesRegex(ValueError,'TITLE_SHORTEN_REQUIRED'):apply(build_visual_plan(36,oversized,[],asset=asset),oversized,max_lines=2)

    def test_30_sources_15_outputs_keep_full_intervals_and_three_distinct_scripts(self):
        durations=[13.42,15.37,11.,16.1,13.53,14.57,12.61,14.61,12.01,10.1,12.67,14.51,13.,15.07,12.9,11.83,12.07,15.49,12.61,17.93,32.97,15.7,12.7,23.53,17.67,13.33,16.73,14.03,15.33,18.83]
        sources=[dict(rank=n,duration=d,path=f'{n}.mp4',sha256=str(n),filename=f'{n}.mp4') for n,d in enumerate(durations,1)]
        analysis={s['rank']:dict(clean_script='Original',context='Paired car ornaments',selling_points=['paired design'],allowed_claims=['paired design'],overlays_need_review=False,existing_arrow=False,source_language='zh',voice_quality='GOOD') for s in sources}
        chosen=selection(sources,analysis,15)
        self.assertEqual(len(chosen['outputs']),15)
        self.assertTrue(all(2<=len(x['clips'])<=12 for x in chosen['outputs']))
        for s in sources:
            spans=sorted((c['start'],c['end']) for x in chosen['outputs'] for c in x['clips'] if c['source_rank']==s['rank'])
            self.assertAlmostEqual(spans[0][0],0.)
            self.assertAlmostEqual(spans[-1][1],s['duration'])
            for left,right in zip(spans,spans[1:]):self.assertAlmostEqual(left[1],right[0])
        items,mapped=remix_items(chosen,sources,analysis,15,selling_points_primary=True)
        self.assertLess(max(x['duration'] for x in items)-min(x['duration'] for x in items),5.)
        self.assertAlmostEqual(sum(x['duration'] for x in items),sum(durations))
        groups=[dict(ranks=list(range(1,6)),angle='LOVE_GIFT_DAILY_COMPANION'),dict(ranks=list(range(6,11)),angle='LOVE_GIFT_SYMBOL'),dict(ranks=list(range(11,16)),angle='LOVE_GIFT_THOUGHTFUL_GESTURE')]
        job=dict(mode='Remix',output_count=15,market='Brazil',custom_remix=dict(coverage='ALL_SOURCES_BALANCED',script_groups=groups))
        job['custom_remix']=validate(job)
        ctx,mapping,_=contexts(job,items,mapped,dict(product_name='Couple ornament'),'pt-BR','')
        self.assertEqual(len(ctx),3)
        self.assertEqual(len(set(c['clean_script'] for c in ctx)),3)
        self.assertTrue(all('sunflower' not in c['clean_script'].lower() for c in ctx))
        for ranks in [range(1,6),range(6,11),range(11,16)]:self.assertEqual(len({mapping[r] for r in ranks}),1)
        self.assertEqual(len(set(mapping.values())),3)


if __name__=='__main__':unittest.main()
