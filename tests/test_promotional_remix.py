"""User-supplied offers and complete source coverage stay scoped to the job."""
import copy
import tempfile
import unittest
from pathlib import Path
import autocut.context
from autocut.custom_remix import validate, selection, contexts, snapshot
from autocut.remix import remix_items
from autocut.task_instructions import effective


class PromotionalRemixTests(unittest.TestCase):
    def spec(self):
        return dict(mode='Remix',market='Mexico',output_count=15,custom_remix=dict(
            coverage='ALL_SOURCES_BALANCED',script_groups=[dict(ranks=[n],angle='HARD_SELL_PROMOTION') for n in range(1,16)],
            cut_style='FAST_INTERLEAVED',promotion_offer='Por tiempo limitado: 60% de descuento y envío gratis.'))

    def sources(self):
        source=[dict(rank=n,duration=26.+n/10,path=f'{n}.mp4',sha256=str(n),filename=f'{n}.mp4') for n in range(1,31)]
        analysis={s['rank']:dict(clean_script='A luggage strap joins bags.',context='Strap joining travel luggage',selling_points=['connects luggage'],allowed_claims=['connects luggage'],overlays_need_review=False,existing_arrow=False,source_language='en',voice_quality='GOOD') for s in source}
        return source,analysis

    def test_all_thirty_sources_reach_fifteen_fast_timelines_with_valid_nonoverlapping_windows(self):
        source,analysis=self.sources();before=copy.deepcopy(source)
        chosen=selection(source,analysis,15,fast_interleaved=True);items,mapped=remix_items(chosen,source,analysis,15,selling_points_primary=True)
        used={c['source_rank'] for item in items for c in item['remix_clips']}
        self.assertEqual(used,set(range(1,31)));self.assertEqual(source,before)
        for item in items:
            clips=item['remix_clips'];self.assertEqual(len(clips),12)
            self.assertLessEqual(item['duration'],36.00001)
            self.assertTrue(all(.5<=c['source_end']-c['source_start']<=3.00001 for c in clips))
            self.assertTrue(all(a['source_rank']!=b['source_rank'] for a,b in zip(clips,clips[1:])))
            for rank in {c['source_rank'] for c in clips}:
                windows=sorted((c['source_start'],c['source_end']) for c in clips if c['source_rank']==rank)
                self.assertTrue(all(a[1]<=b[0]+1e-9 for a,b in zip(windows,windows[1:])))

    def test_fifteen_distinct_spanish_contexts_and_titles_receive_only_the_supplied_offer(self):
        source,analysis=self.sources();items,mapped=remix_items(selection(source,analysis,15,True),source,analysis,15,True)
        job=self.spec();job['custom_remix']=validate(job)
        rows,mapping,groups=contexts(job,items,mapped,dict(product_name='行李箱绑带'),'es-MX','Opening hook')
        self.assertEqual(len(rows),15);self.assertEqual(len(set(mapping.values())),15)
        for row in rows:
            self.assertEqual(row['language'],'es-MX');self.assertEqual(row['market'],'Mexico')
            self.assertIn('Mexican Spanish',row['clean_script'])
            self.assertIn(job['custom_remix']['promotion_offer'],row['promotion_context'])
            self.assertIn(job['custom_remix']['promotion_offer'],row['allowed_claims'])
            self.assertIn(job['custom_remix']['promotion_offer'],row['title_brief'])
            self.assertNotIn('romantic',row['clean_script'])
        changed=copy.deepcopy(job);changed['custom_remix']['promotion_offer']='Solo envío gratis.'
        other=contexts(changed,items,mapped,dict(product_name='行李箱绑带'),'es-MX','')[0]
        self.assertTrue(set(row['script_group_id'] for row in rows).isdisjoint(row['script_group_id'] for row in other))

    def test_new_offer_requires_explicit_promotional_group_and_does_not_change_legacy_snapshot(self):
        spec=self.spec();saved=snapshot(spec);self.assertEqual(effective(saved,1)['copy_style'],'hard_sell');self.assertEqual(effective(saved,1)['video_pace'],'slightly_fast')
        for change in [dict(promotion_offer=''),dict(music_only_ranks=[15],music_paths={'15':'song.mp3'})]:
            bad=self.spec();bad['custom_remix'].update(change)
            with self.assertRaises(ValueError):validate(bad)
        legacy=dict(mode='Remix',market='Brazil',output_count=2,custom_remix=dict(coverage='ALL_SOURCES_BALANCED',script_groups=[dict(ranks=[1,2],angle='LOVE_GIFT_DAILY_COMPANION')]))
        result=snapshot(legacy);self.assertEqual(effective(result,1)['copy_style'],'emotional')
        self.assertNotIn('video_pace',effective(result,1))
        legacy['custom_remix']['promotion_offer']='unrequested offer'
        with self.assertRaises(ValueError):validate(legacy)

    def test_submission_keeps_real_approval_barriers_and_new_framing(self):
        from fastapi.testclient import TestClient
        from autocut.server import create_app
        from autocut.storage import Store
        from autocut.source_frame import AUTO_POLICY
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store=Store(root/'data')
            class Manager:
                def __init__(self):self.started=[]
                def enqueue(self,ident):self.started.append(ident)
            manager=Manager();client=TestClient(create_app(store,None,None,manager,'fixture',recover_on_startup=False),base_url='http://127.0.0.1:9000',headers={'Authorization':'Bearer fixture'})
            client.post('/native/grant',json={'path':str(root)})
            spec=dict(self.spec(),request_id='promotional-remix-fixture',input_path=str(root),product_name='行李箱绑带',product_profile_id='general',watermark_mode='handled')
            response=client.post('/jobs',json=spec);self.assertEqual(response.status_code,200,response.text)
            job=store.get(response.json()['id']);self.assertEqual(job['target_language'],'es-MX')
            self.assertTrue(job['content_confirmation_required']);self.assertTrue(job['batch_review_required'])
            self.assertFalse(job.get('scripts_approved'));self.assertFalse(job.get('titles_approved'))
            self.assertEqual(job['source_frame_mode'],AUTO_POLICY)
            self.assertEqual(client.post('/jobs',json=spec).json()['id'],job['id']);self.assertEqual(len(manager.started),1)


if __name__=='__main__':unittest.main()
