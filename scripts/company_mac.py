#!/usr/bin/env python3
"""Deployment only. Never runs production jobs, clears data, or accesses provider secrets."""
import argparse,fcntl,hashlib,json,os,platform,re,shutil,sqlite3,subprocess,sys,tarfile,tempfile,time,urllib.request
from pathlib import Path
B=Path(__file__).resolve().parents[1]
DATA=Path(os.environ.get('AUTOCUT_DATA_HOME',Path.home()/'Library/Application Support/AutoCut'))
APP=Path(os.environ.get('CF_INSTALL_APP',Path.home()/'Applications/Creative Factory.app'))
def read(p):return json.loads(Path(p).read_text())
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()
def run(args,**kw):return subprocess.run(list(map(str,args)),check=True,**kw)
def version_key(v):
 if not re.fullmatch(r'\d+\.\d+\.\d+',v):raise ValueError('INVALID_RELEASE_VERSION')
 return tuple(map(int,v.split('.')))
def version(app):return read(Path(app)/'Contents/Resources/version.json')
def validate_upgrade(old,new):
 if old and (version_key(new['version']),new['build_id']) < (version_key(old['version']),old['build_id']):raise ValueError('DOWNGRADE_BLOCKED')
 if new['schema_version']!=3:raise ValueError('MIGRATION_NOT_APPROVED: only schema 3 deployment is supported')
def db_info(home):
 db=Path(home).resolve()/'autocut.sqlite3'
 if not db.exists():return {'schema':0,'active':0}
 with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
  schema=c.execute('select max(version) from migrations').fetchone()[0]
  active=c.execute("select count(*) from jobs where state in ('RUNNING','QUEUED','STOPPING') and deleted_at is null").fetchone()[0]
  for (raw,) in c.execute('select data from job_items'):
   item=json.loads(raw)
   if item.get('operation_inflight') or item.get('status') in {'RUNNING','QUEUED','RENDERING'}:active+=1
 return {'schema':schema,'active':active}
def check_idle(home=DATA):
 state=db_info(home)
 if state['active']:raise ValueError('Creative Factory仍有运行中任务，请任务完成后再更新。')
 return state
def validate_extract(tar,dest):
 base=dest.resolve()
 for m in tar.getmembers():
  p=base/m.name
  if Path(m.name).is_absolute() or not p.resolve().is_relative_to(base):raise ValueError('UNSAFE_ARCHIVE_PATH')
  if m.isdev() or m.isfifo() or m.islnk():raise ValueError('UNSAFE_ARCHIVE_TYPE')
  if m.issym() and (Path(m.linkname).is_absolute() or not (p.parent/m.linkname).resolve().is_relative_to(base)):raise ValueError('UNSAFE_ARCHIVE_LINK')
 # All member paths and link destinations were checked before extraction.
 if sys.version_info >= (3,12):tar.extractall(dest,filter='data')
 else:tar.extractall(dest)
def environment():
 if platform.system()!='Darwin' or platform.machine()!='arm64':raise ValueError('APPLE_SILICON_MAC_REQUIRED')
 ver=subprocess.check_output(['sw_vers','-productVersion'],text=True).strip()
 if int(ver.split('.')[0])<27:raise ValueError('MACOS_27_REQUIRED')
 run(['xcrun','--find','swiftc'],stdout=subprocess.DEVNULL)
 run(['git','--version'],stdout=subprocess.DEVNULL)
 print('macOS '+ver+' / arm64 / Swift / Git ready')
def verify_runtime(root):
 lock=read(B/'manifests/runtime-lock.json')
 for row in lock['files']:
  p=root/row['path']
  if 'symlink' in row:
   if not p.is_symlink() or p.readlink().as_posix()!=row['symlink']:raise ValueError('RUNTIME_LINK_MISMATCH: '+row['path'])
  elif not p.is_file() or sha(p)!=row['sha256']:raise ValueError('RUNTIME_HASH_MISMATCH: '+row['path'])
def setup():
 environment();lock=read(B/'manifests/runtime-lock.json');archive=B/lock['archive']['path']
 if not archive.is_file() or sha(archive)!=lock['archive']['sha256']:
  raise ValueError('RUNTIME_LFS_MISSING: run git lfs pull using this repository SSH deploy key')
 stage=B/'staging';stage.mkdir(exist_ok=True)
 # Verify before reuse. A changed lock yields a fresh extraction, not a dependency upgrade.
 try:verify_runtime(stage)
 except (OSError,ValueError):
  with tempfile.TemporaryDirectory(prefix='cf-runtime-',dir=B/'build') as d:
   temp=Path(d)
   with tarfile.open(archive) as t:validate_extract(t,temp)
   verify_runtime(temp)
   for name in ['python','main','media']:
    dst=stage/'runtime'/name;dst.parent.mkdir(exist_ok=True)
    if dst.exists():shutil.rmtree(dst)
    shutil.move(str(temp/'runtime'/name),dst)
   if (stage/'models').exists():shutil.rmtree(stage/'models')
   shutil.move(str(temp/'models'),stage/'models')
 print('Runtime verified; ProPainter excluded (non-commercial license).')
 font=Path(os.environ.get('CF_LICENSED_FONT',B/'local-resources/Mathilde-BlackItalic.ttf'))
 if not font.is_file():print('Required Mathilde font not provisioned. Full App build is blocked; no substitute font will be used.')
def build(candidate=False):
 setup();channel=read(B/'manifests/production.json')
 if not candidate and not channel.get('approved'):raise ValueError('RELEASE_NOT_APPROVED: use --candidate only for isolated acceptance')
 installed_font=APP/'Contents/Resources/engine/assets/fonts/Mathilde-BlackItalic.ttf'
 if 'CF_LICENSED_FONT' not in os.environ and installed_font.is_file():os.environ['CF_LICENSED_FONT']=str(installed_font)
 py=B/'staging/runtime/python/bin/python3.12';run([py,B/'scripts/build.py','--company'])
 app=Path(read(B/'build/latest-build.json')['path']);verify_app(app);smoke(app)
 return app
def verify_app(app):
 app=Path(app).resolve();r=app/'Contents/Resources';v=version(app)
 if v['compatibility']['bundle_id']!='com.autocut.desktop' or v['compatibility']['keychain_service']!='com.autocut.providers':raise ValueError('COMPATIBILITY_ID_CHANGED')
 manifest=read(app.parent/'BUILD_MANIFEST.json')
 for row in manifest['files']:
  p=app/row['path']
  if not p.is_file() or sha(p)!=row['sha256']:raise ValueError('APP_RESOURCE_HASH_MISMATCH: '+row['path'])
 run(['codesign','--verify','--deep','--strict',app],capture_output=True)
 native=[]
 for f in r.rglob('*'):
  if f.is_file() and not f.is_symlink():
   with f.open('rb') as stream:magic=stream.read(4)
   if magic in {b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe',b'\xca\xfe\xba\xbf'}:native.append(str(f))
 linkage=subprocess.check_output(['otool','-L',*native],text=True)
 if any(line.startswith('\t') and line.strip().startswith(('/Users/','/opt/homebrew/','/usr/local/')) for line in linkage.splitlines()):raise ValueError('NONPORTABLE_NATIVE_DEPENDENCY')
 for name in ['Mathilde-BlackItalic.ttf','Poppins-Bold.ttf']:
  if not (r/'engine/assets/fonts'/name).is_file():raise ValueError('FONT_REQUIRED: '+name)
 for cmd in [[r/'runtime/python/bin/python3.12','--version'],[r/'runtime/media/bin/ffmpeg','-version'],[r/'runtime/media/bin/ffprobe','-version'],[r/'runtime/media/bin/whisper-cli','--help']]:run(cmd,capture_output=True)
 print('Signature, resource hashes, runtime, fonts verified.')
def api(home,path,post=False):
 state=read(Path(home)/'runtime/backend.json')
 request=urllib.request.Request('http://127.0.0.1:'+str(state['port'])+path,data=b'{}' if post else None,headers={'Authorization':'Bearer '+state['token'],'Content-Type':'application/json'})
 with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
def smoke(app):
 r=Path(app).resolve()/'Contents/Resources'
 with tempfile.TemporaryDirectory(prefix='cf-company-smoke-') as d:
  home=Path(d);env=dict(os.environ,AUTOCUT_RESOURCES=str(r),AUTOCUT_DATA_HOME=d,PYTHONPATH=str(r/'backend')+':'+str(r/'runtime/main'),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
  # No settings/provider endpoints are called. No production DB or Keychain writes.
  run([r/'runtime/python/bin/python3.12','-B','-c','from autocut.pipeline import Manager; from autocut.drivers import OpenAIDriver; from app.s3 import headless_media; print("Engine / OpenAI adapter imports PASS")'],env=env,capture_output=True)
  with (home/'backend.log').open('w') as log:
   worker=subprocess.Popen([r/'runtime/python/bin/python3.12','-B','-m','autocut.server'],env=env,stdout=log,stderr=log)
   try:
    for _ in range(100):
     if worker.poll() is not None:raise ValueError('ISOLATED_BACKEND_START_FAILED: '+(home/'backend.log').read_text()[-1600:])
     try:
      if api(home,'/runtime/active')['count']==0:break
     except (OSError,ValueError):time.sleep(.2)
    else:raise ValueError('ISOLATED_BACKEND_TIMEOUT')
    if db_info(home)['schema']!=3:raise ValueError('SCHEMA_SMOKE_FAILED')
    api(home,'/runtime/shutdown',True);worker.wait(timeout=15)
   finally:
    if worker.poll() is None:worker.terminate();worker.wait(timeout=10) # isolated test process only
  # Real short encode/decode using bundled FFmpeg, not a mock.
  ff=r/'runtime/media/bin/ffmpeg';out=home/'smoke.mp4'
  run([ff,'-v','error','-f','lavfi','-i','testsrc2=s=180x320:r=30:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',out],capture_output=True)
  run([ff,'-v','error','-xerror','-i',out,'-f','null','-'],capture_output=True)
 print('Isolated Backend / schema / short FFmpeg encode-decode PASS (no paid APIs).')
def normal_close():
 # Native normal quit invokes the existing app's idle/stop safeguards. Never SIGKILL a worker.
 helper=B/'build/company_quit';source=B/'scripts/company_quit.swift'
 run(['xcrun','swiftc',source,'-o',helper],capture_output=True);run([helper],capture_output=True)
 deadline=time.monotonic()+25
 while time.monotonic()<deadline:
  p=subprocess.run([helper,'--check'],capture_output=True)
  if p.returncode==0:return
  time.sleep(.25)
 raise ValueError('APP_STILL_RUNNING: close normally after tasks finish; update aborted')
def backup_db(destination):
 db=DATA/'autocut.sqlite3'
 if db.exists():
  with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(destination/'autocut.sqlite3') as dst:src.backup(dst)
def install(app,candidate=False):
 app=Path(app).resolve();verify_app(app);smoke(app);new=version(app);old=version(APP) if APP.exists() else None;validate_upgrade(old,new)
 if not candidate and not read(B/'manifests/production.json').get('approved'):raise ValueError('RELEASE_NOT_APPROVED')
 DATA.mkdir(parents=True,exist_ok=True);(DATA/'runtime').mkdir(exist_ok=True)
 with (DATA/'runtime/company-update.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);before=check_idle();normal_close();check_idle()
  # Backend lock excludes any worker during the filesystem swap.
  with (DATA/'runtime/backend.lock').open('a') as gate:
   fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
   check_idle()
   if before['schema'] not in {0,3}:raise ValueError('DATABASE_SCHEMA_REQUIRES_VERIFIED_MIGRATION')
   stamp=time.strftime('%Y%m%d-%H%M%S');archive=DATA/'updates'/stamp;archive.mkdir(parents=True);backup_db(archive)
   APP.parent.mkdir(parents=True,exist_ok=True);incoming=APP.parent/('.Creative Factory.incoming-'+stamp+'.app')
   shutil.copytree(app,incoming,symlinks=True);run(['codesign','--verify','--deep','--strict',incoming],capture_output=True)
   previous=archive/'Creative Factory.app'
   if APP.exists():APP.rename(previous)
   try:incoming.rename(APP)
   except BaseException:
    if previous.exists():previous.rename(APP)
    raise
   shutil.copy2(app.parent/'BUILD_MANIFEST.json',archive/'installed-manifest.json')
   receipt=dict(previous=str(previous) if previous.exists() else None,installed=str(APP),version=new,schema_before=before['schema'],database_backup=str(archive/'autocut.sqlite3'))
   (DATA/'updates/latest.json').write_text(json.dumps(receipt,indent=2))
 run(['open',APP])
 for _ in range(100):
  try:
   state=read(DATA/'runtime/backend.json')
   if Path(state.get('resources','')).resolve()==(APP/'Contents/Resources').resolve():api(DATA,'/runtime/active');print('Installed and Backend ready:',APP);return
  except (OSError,ValueError):pass
  time.sleep(.3)
 raise ValueError('NEW_APP_START_FAILED: previous app preserved; run rollback_company_mac.sh (schema check enforced)')
def rollback():
 receipt=read(DATA/'updates/latest.json');previous=Path(receipt.get('previous') or '/nonexistent')
 if not previous.is_dir():raise ValueError('NO_PREVIOUS_APP')
 old=version(previous);state=check_idle()
 if state['schema']!=old['schema_version']:raise ValueError('ROLLBACK_SCHEMA_INCOMPATIBLE: no automatic database restore')
 run(['codesign','--verify','--deep','--strict',previous],capture_output=True)
 normal_close();check_idle()
 with (DATA/'runtime/backend.lock').open('a') as gate:
  fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
  failed=APP.parent/('Creative Factory.failed-'+time.strftime('%Y%m%d-%H%M%S')+'.app');APP.rename(failed)
  try:shutil.copytree(previous,APP,symlinks=True)
  except BaseException:
   if APP.exists():shutil.rmtree(APP)
   failed.rename(APP);raise
 run(['open',APP]);print('Previous App restored. Database and Keychain untouched.')
def update():
 check_idle()
 if subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=B,text=True).strip():raise ValueError('LOCAL_SOURCE_CHANGES: update aborted')
 run(['git','fetch','origin','main','--tags'],cwd=B)
 channel=json.loads(subprocess.check_output(['git','show','origin/main:manifests/production.json'],cwd=B,text=True))
 tag=channel.get('tag','')
 if not channel.get('approved') or not re.fullmatch(r'v\d+\.\d+\.\d+',tag):raise ValueError('NO_ACCEPTED_PRODUCTION_RELEASE')
 target=json.loads(subprocess.check_output(['git','show',tag+':manifests/latest-build.json'],cwd=B,text=True))
 if APP.exists():
  current=version(APP);validate_upgrade(current,target)
  if (current['version'],current['build_id'])==(target['version'],target['build_id']):print('Already current.');return
 # Build in a detached release worktree; never run unaccepted main code or overwrite a developer checkout.
 folder=B/'build'/('release-'+tag)
 if not folder.exists():run(['git','worktree','add','--detach',folder,tag],cwd=B)
 run(['git','lfs','pull'],cwd=folder)
 env=dict(os.environ,CF_RELEASE_BUILD_ID=target['build_id'])
 run(['bash',folder/'scripts/build_company_mac.sh'],env=env)
 built=read(folder/'build/latest-build.json')['path']
 run(['bash',folder/'scripts/install_company_mac.sh',built],env=env)
def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['setup','build','verify','smoke','install','update','rollback','idle']);p.add_argument('app',nargs='?');p.add_argument('--candidate',action='store_true');a=p.parse_args();(B/'build').mkdir(exist_ok=True)
 if a.action=='setup':setup()
 elif a.action=='build':build(a.candidate)
 elif a.action=='verify':verify_app(a.app)
 elif a.action=='smoke':smoke(a.app)
 elif a.action=='install':install(a.app or read(B/'build/latest-build.json')['path'],a.candidate)
 elif a.action=='update':update()
 elif a.action=='rollback':rollback()
 else:print(json.dumps(check_idle()))
if __name__=='__main__':
 try:main()
 except (ValueError,OSError,subprocess.CalledProcessError) as exc:print(str(exc),file=sys.stderr);sys.exit(1)
