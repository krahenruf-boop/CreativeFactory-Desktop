import copy
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
from autocut import file_storage as storage


class FileStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.patch=patch.object(storage,'storage_root',lambda:self.root/'fixed')
        self.patch.start();self.addCleanup(self.patch.stop)

    def test_source_copy_idempotence_and_checkpoint_preservation(self):
        tmp_path=self.root
        source = tmp_path / 'Downloads';source.mkdir()
        (source / '01.mp4').write_bytes(b'fixture-original')
        (source / '02.mp4').write_bytes(b'unselected')
        original = storage.sha256(source / '01.mp4')
        class Store:
            def __init__(self):
                self.job = dict(id='a'*32, input_path=str(source), source_names=['01.mp4'],
                                storage_policy='fixed-v1', job_name='测试', final_script='locked',
                                title='locked title', audio_result={'fingerprint': 'paid-cache'})
            def update(self, id, **fields):self.job.update(fields)
            def get(self, id):return copy.deepcopy(self.job)
        store = Store();first = storage.preserve_job_sources(store, store.get('a'*32))
        assert Path(first['input_path']).parent == tmp_path/'fixed/Media'
        assert [p.name for p in Path(first['input_path']).iterdir()] == ['01.mp4']
        assert storage.sha256(Path(first['input_path'])/'01.mp4') == original
        assert storage.sha256(source/'01.mp4') == original
        assert first['final_script'] == 'locked' and first['title'] == 'locked title'
        assert first['audio_result']['fingerprint'] == 'paid-cache'
        assert storage.preserve_job_sources(store, first) == first
        legacy = dict(first, storage_policy=None)
        assert storage.preserve_job_sources(store, legacy) == legacy


    def test_outputs_supplements_and_explicit_destination(self):
        tmp_path=self.root
        j = dict(id='b'*32, job_name='../测试/任务', input_path=str(tmp_path/'source'))
        assert storage.output_directory(j).parent == tmp_path/'source/outputs'
        assert storage.output_directory(dict(j,original_input_path=str(tmp_path/'original'))).parent == tmp_path/'original/outputs'
        explicit = tmp_path/'my-export'
        assert storage.output_directory(dict(j,output_dir=str(explicit))) == explicit.resolve()
        with self.assertRaisesRegex(ValueError,'OUTPUT_OUTSIDE_SOURCE'):
            storage.output_directory(dict(j,storage_policy='source-v1',output_dir=str(explicit)))
        assert storage.supplement_directory(j['id']) == tmp_path/'fixed/Supplements'/j['id']
        with self.assertRaises(ValueError):storage.supplement_directory('../../another-job')

    def test_new_jobs_keep_original_media_in_place(self):
        job=dict(id='c'*32,input_path=str(self.root/'source'),storage_policy='source-v1')
        store=unittest.mock.Mock()
        assert storage.preserve_job_sources(store,job) == job
        store.update.assert_not_called()
        assert not (self.root/'fixed/Media').exists()


    def test_copy_conflict_preserves_both_files(self):
        tmp_path=self.root
        source=tmp_path/'source';destination=tmp_path/'copy'
        source.write_bytes(b'original');destination.write_bytes(b'other output')
        with self.assertRaisesRegex(ValueError,'STORAGE_PATH_CONFLICT'):
            storage.copy_verified(source,destination)
        assert source.read_bytes()==b'original' and destination.read_bytes()==b'other output'


    def test_long_original_filename_is_not_extended(self):
        tmp_path=self.root
        source=tmp_path/('a'*235+'.mp4');source.write_bytes(b'long-name-fixture')
        destination=tmp_path/'managed'/source.name
        storage.copy_verified(source,destination)
        assert destination.read_bytes()==source.read_bytes()

if __name__=='__main__':unittest.main()
