import argparse,datetime,hashlib,json,os,plistlib,shutil,subprocess,sys
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo
B=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--version');p.add_argument('--build-id');p.add_argument('--company',action='store_true');args=p.parse_args()
(B/'build').mkdir(exist_ok=True)
subprocess.run([sys.executable,str(B/'scripts/stage_engine.py')],check=True)
subprocess.run([sys.executable,str(B/'scripts/compatibility.py')],check=True)
subprocess.run(['xcrun','swift',str(B/'scripts/build_icons.swift'),str(B/'web/creative-factory-mark.svg'),str(B/'build/CreativeFactory.iconset')],check=True)
subprocess.run(['iconutil','-c','icns',str(B/'build/CreativeFactory.iconset'),'-o',str(B/'build/CreativeFactory.icns')],check=True)
for source,target in [('AutoCut.swift','AutoCut'),('Keychain.swift','autocut-keychain')]:
 subprocess.run(['xcrun','swiftc',str(B/'app_shell'/source),'-o',str(B/'build'/target)],check=True)
version=args.version or (B/'AUTOCUT_VERSION').read_text().strip();build=args.build_id or os.environ.get('CF_RELEASE_BUILD_ID') or datetime.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d.%H%M%S')
release=Path(os.environ.get('CF_RELEASE_ROOT',str(B/'build/releases')))/f'CreativeFactory_{version}_{build}'
if release.exists():raise SystemExit('Release already exists; use a new build ID. Old releases are preserved.')
app=release/'Creative Factory.app';content=app/'Contents';res=content/'Resources';mac=content/'MacOS';mac.mkdir(parents=True);res.mkdir()
for name in ['engine','runtime']+([] if args.company else ['third_party']):
 shutil.copytree(B/'staging'/name,res/name,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info','__editable__*','direct_url.json',*(['propainter'] if args.company else [])))
if args.company:(res/'third_party').mkdir()
shutil.copytree(B/'licenses',res/'licenses')
for name in ['backend','web']:shutil.copytree(B/name,res/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copy2(B/'build/CreativeFactory.icns',res/'CreativeFactory.icns')
(res/'bin').mkdir();shutil.copy2(B/'build/AutoCut',mac/'AutoCut');shutil.copy2(B/'build/autocut-keychain',res/'bin/autocut-keychain')
for name,tail in [('propainter-python','"$@"'),('propainter-delogo',"-c 'from propainter_delogo.cli import main; raise SystemExit(main())' \"$@\"")]:
 file=res/'bin'/name;file.write_text('#!/bin/sh\nset -eu\nautocut_resources="$(cd "$(dirname "$0")/.." && pwd)"\nexport PYTHONDONTWRITEBYTECODE=1\nexport PYTHONNOUSERSITE=1\nexport PYTHONPATH="$autocut_resources/runtime/propainter:$autocut_resources/third_party/propainter-delogo"\nexec "$autocut_resources/runtime/python/bin/python3.12" '+tail+'\n');file.chmod(0o755)
baseline=json.loads((B/'manifests/engine-snapshot.json').read_text());engine_fp=baseline['engine_fingerprint'];current_commit=baseline['engine_commit']
v=dict(product_name='Creative Factory',compatibility=dict(previous_product_name='AutoCut',bundle_id='com.autocut.desktop',data_directory='~/Library/Application Support/AutoCut',keychain_service='com.autocut.providers'),version=version,build_id=build,engine_version=current_commit,engine_fingerprint=engine_fp,schema_version=3,minimum_schema=1,architecture='arm64',minimum_macos='27.0',signing='AD_HOC_SIGNED',notarized=False,distribution_profile='company' if args.company else 'personal',propainter_enabled=not args.company)
(res/'version.json').write_text(json.dumps(v,indent=2))
info=dict(CFBundleName='Creative Factory',CFBundleDisplayName='Creative Factory',CFBundleIdentifier='com.autocut.desktop',CFBundleExecutable='AutoCut',CFBundlePackageType='APPL',CFBundleShortVersionString=version,CFBundleVersion=build,LSMinimumSystemVersion='27.0',NSHighResolutionCapable=True,NSAppTransportSecurity={'NSAllowsLocalNetworking':True},NSDocumentsFolderUsageDescription='用于读取您选择的素材并保存成片',NSDownloadsFolderUsageDescription='用于读取您选择的素材并保存成片')
info['CFBundleIconFile']='CreativeFactory.icns'
(content/'Info.plist').write_bytes(plistlib.dumps(info))
# Sign every native dependency, then the bundle. Never sign or alter source binaries.
macho={b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe',b'\xca\xfe\xba\xbf',b'\xfe\xed\xfa\xcf'}
for f in res.rglob('*'):
 if f.is_file() and not f.is_symlink():
  with f.open('rb') as h:magic=h.read(4)
  if magic in macho:subprocess.run(['codesign','--force','--sign','-',str(f)],check=True,capture_output=True)
subprocess.run(['codesign','--force','--deep','--sign','-',str(app)],check=True,capture_output=True)
subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True,capture_output=True)
dependencies={part:sorted([dict(name=d.metadata['Name'],version=d.version) for d in metadata.distributions(path=[str(res/'runtime'/part)])],key=lambda d:d['name'].lower()) for part in ['main','propainter']}
upstream=json.loads((B/'manifests/runtime-lock.json').read_text()).get('upstream_commits',{})
manifest=dict(v,dependencies=dependencies,upstream_commits=upstream,python=sys.version.split()[0],files=[],packaging_files=[])
for name in ['app_shell','backend','web','scripts']:
 for file in sorted((B/name).rglob('*')):
  if file.is_file() and file.suffix not in {'.pyc'} and '__pycache__' not in file.parts:
   manifest['packaging_files'].append(dict(path=str(file.relative_to(B)),sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
for f in sorted(res.rglob('*')):
 if f.is_file() and not f.is_symlink():
  with f.open('rb') as h:digest=hashlib.file_digest(h,'sha256').hexdigest()
  manifest['files'].append(dict(path=str(f.relative_to(app)),size=f.stat().st_size,sha256=digest))
# Development reference images must never be included in the distributable.
reference_hashes={hashlib.sha256(f.read_bytes()).hexdigest() for f in (B/'docs/ui_reference').glob('*.png')}
assert not any(row['sha256'] in reference_hashes for row in manifest['files']), 'UI_REFERENCE_IN_RELEASE'
assert not (res/'docs/ui_reference').exists()
assert not (res/'docs/brand_reference').exists()
brand_ref=B/'docs/brand_reference/creative-factory-reference.png'
if brand_ref.exists():assert not any(row['sha256']==hashlib.sha256(brand_ref.read_bytes()).hexdigest() for row in manifest['files'])
(release/'BUILD_MANIFEST.json').write_text(json.dumps(manifest,indent=2))
for name in ['README_FIRST_RUN.md','RELEASE_NOTES.md','VERIFY.md','TASK_CUSTOM_INSTRUCTIONS.md','DEVELOPMENT_AND_RELEASE.md','WORKBENCH.md','COPY_PRESENTATION.md','BRAND_COMPATIBILITY.md','TASK_DELETION.md','REVIEW_FLOW.md','FINAL_SCRIPT_LANGUAGE.md','APPROVAL_STATES.md','CONTENT_CONFIRMATION.md','BATCH_WORKSPACE.md','AUTOMATIC_PRODUCTION.md']:
 source=B/'docs'/name
 if source.exists():shutil.copy2(source,release/name)
(B/'build/latest-build.json').write_text(json.dumps(dict(path=str(app),release=str(release),**v),indent=2))
print(json.dumps(dict(path=str(app),release=str(release),**v),indent=2))
