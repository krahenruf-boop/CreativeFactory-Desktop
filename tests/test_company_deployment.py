"""Deployment safety regressions. Temp databases only; no providers or production jobs."""
import io,json,sqlite3,tarfile,tempfile,unittest,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import company_mac as c
class Deployment(unittest.TestCase):
 def setUp(self):self.t=tempfile.TemporaryDirectory();self.home=Path(self.t.name)
 def tearDown(self):self.t.cleanup()
 def db(self,state='COMPLETED',item=None,schema=3):
  with sqlite3.connect(self.home/'autocut.sqlite3') as d:
   d.executescript('create table migrations(version integer);create table jobs(state text,deleted_at real);create table job_items(data text);')
   d.execute('insert into migrations values(?)',(schema,));d.execute('insert into jobs values(?,null)',(state,));d.execute('insert into job_items values(?)',(json.dumps(item or {'status':'PASS'}),))
 def test_running_rejected_without_mutation(self):
  self.db('RUNNING');before=(self.home/'autocut.sqlite3').read_bytes()
  with self.assertRaises(ValueError):c.check_idle(self.home)
  self.assertEqual(before,(self.home/'autocut.sqlite3').read_bytes())
 def test_item_worker_blocks_even_parent_is_review(self):
  self.db('REVIEW_REQUIRED',{'operation_inflight':True})
  with self.assertRaises(ValueError):c.check_idle(self.home)
 def test_completed_idle(self):
  self.db();self.assertEqual(c.check_idle(self.home),{'schema':3,'active':0})
 def test_fresh_machine_no_database_initialization(self):
  self.assertEqual(c.db_info(self.home),{'schema':0,'active':0});self.assertEqual(list(self.home.iterdir()),[])
 def test_downgrade_rejected(self):
  with self.assertRaisesRegex(ValueError,'DOWNGRADE'):c.validate_upgrade({'version':'0.1.18','build_id':'20261009.000000'},{'version':'0.1.17','build_id':'20261010.000000','schema_version':3})
 def test_same_version_older_build_rejected(self):
  with self.assertRaises(ValueError):c.validate_upgrade({'version':'0.1.17','build_id':'20261008.145239'},{'version':'0.1.17','build_id':'20261008.123521','schema_version':3})
 def test_unverified_migration_rejected(self):
  with self.assertRaisesRegex(ValueError,'MIGRATION'):c.validate_upgrade(None,{'version':'0.1.18','build_id':'1','schema_version':4})
 def test_schema_mismatch_blocks_rollback_before_quit(self):
  self.db(schema=4);app=self.home/'previous.app';(app/'Contents/Resources').mkdir(parents=True);(app/'Contents/Resources/version.json').write_text(json.dumps({'schema_version':3}));updates=self.home/'updates';updates.mkdir();(updates/'latest.json').write_text(json.dumps({'previous':str(app)}))
  with patch.object(c,'DATA',self.home),patch.object(c,'check_idle',return_value=c.db_info(self.home)),patch.object(c,'normal_close') as quit:
   with self.assertRaisesRegex(ValueError,'SCHEMA_INCOMPATIBLE'):c.rollback()
   quit.assert_not_called()
 def test_database_backup_preserves_rows(self):
  self.db();out=self.home/'backup';out.mkdir()
  with patch.object(c,'DATA',self.home):c.backup_db(out)
  self.assertEqual(c.db_info(out),c.db_info(self.home))
 def archive(self,name,link=None):
  buf=io.BytesIO()
  with tarfile.open(fileobj=buf,mode='w') as t:
   m=tarfile.TarInfo(name)
   if link:m.type=tarfile.SYMTYPE;m.linkname=link
   else:m.size=1
   t.addfile(m,None if link else io.BytesIO(b'x'))
  buf.seek(0);return tarfile.open(fileobj=buf)
 def test_archive_traversal_rejected(self):
  with self.archive('../outside') as t:
   with self.assertRaises(ValueError):c.validate_extract(t,self.home)
 def test_archive_symlink_escape_rejected(self):
  with self.archive('bin/escape','../../outside') as t:
   with self.assertRaises(ValueError):c.validate_extract(t,self.home)
 def test_archive_valid(self):
  with self.archive('runtime/main/file') as t:c.validate_extract(t,self.home)
  self.assertEqual((self.home/'runtime/main/file').read_text(),'x')
 def test_snapshot_integrity(self):
  m=c.read(c.B/'manifests/engine-snapshot.json')
  for row in m['files']:self.assertEqual(c.sha(c.B/'vendor/engine'/row['path']),row['source_sha256'])
 def test_runtime_excludes_restricted_resources(self):
  m=c.read(c.B/'manifests/runtime-lock.json')
  self.assertFalse(any('propainter' in r['path'].lower() or 'Mathilde' in r['path'] for r in m['files']))
 def test_production_channel_not_fabricated(self):self.assertFalse(c.read(c.B/'manifests/production.json')['approved'])
if __name__=='__main__':unittest.main()
