#!/usr/bin/env python3
"""Maintainer-only: freeze the already accepted runtime; never include user data or ProPainter."""
import hashlib,json,shutil,tarfile,subprocess,re
from pathlib import Path
B=Path(__file__).resolve().parents[1]
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
 out=B/'distribution/runtime-macos-arm64-0.1.17.tar.gz';stage=B/'staging'
 # Remove uv's developer-specific dylib identity before freezing copied binaries.
 lib=stage/'runtime/python/lib/libpython3.12.dylib'
 subprocess.run(['install_name_tool','-id','@rpath/libpython3.12.dylib',str(lib)],check=True)
 subprocess.run(['codesign','--force','--sign','-',str(lib)],check=True,capture_output=True)
 roots=['runtime/python','runtime/main','runtime/media','models']
 rows=[]
 for root in roots:
  for p in sorted((stage/root).rglob('*')):
   if '__pycache__' in p.parts or p.suffix=='.pyc' or p.name in {'direct_url.json'}:continue
   if p.is_symlink():
    if not p.resolve().is_relative_to(stage.resolve()):raise ValueError('EXTERNAL_RUNTIME_SYMLINK: '+str(p))
    rows.append(dict(path=str(p.relative_to(stage)),symlink=p.readlink().as_posix()))
   elif p.is_file():rows.append(dict(path=str(p.relative_to(stage)),sha256=sha(p),size=p.stat().st_size))
 with tarfile.open(out,'w:gz',compresslevel=6) as t:
  for row in rows:t.add(stage/row['path'],arcname=row['path'],recursive=False)
 media=[]
 for r in json.loads((B/'manifests/media-runtime.json').read_text()):
  parts=Path(r['source']).parts
  if 'Cellar' in parts:
   root=Path(*parts[:parts.index('Cellar')+3]); formula=next((root/'.brew').glob('*.rb'))
   if any(x['name']==formula.stem for x in media):continue
   target=B/'licenses/media'/formula.stem;target.mkdir(parents=True,exist_ok=True);shutil.copy2(formula,target/formula.name)
   for f in root.iterdir():
    if f.is_file() and (f.name.startswith(('COPYING','LICENSE')) or f.name=='sbom.spdx.json'):shutil.copy2(f,target/f.name)
   text=formula.read_text();url=re.search(r'^  url "([^"]+)"',text,re.M);h=re.search(r'^  sha256 "([^"]+)"',text,re.M)
   media.append(dict(name=formula.stem,version=root.name,source_url=url[1] if url else None,source_sha256=h[1] if h else None,build_recipe=str(target.relative_to(B)/formula.name)))
 upstream={}
 for name,p in [('whisper',Path.home()/'CreativeFactory/.tools/whisper.cpp')]:
  upstream[name]=subprocess.check_output(['git','-C',str(p),'rev-parse','HEAD'],text=True).strip()
 lock=dict(format=1,architecture='arm64',minimum_macos='27.0',base_version='0.1.17',python='3.12.14',distribution='git-lfs-over-repository-ssh',archive=dict(path=str(out.relative_to(B)),sha256=sha(out),size=out.stat().st_size),files=rows,media_sources=media,upstream_commits=upstream,excluded=['ProPainter','torch','personal data','Mathilde font'])
 (B/'manifests/runtime-lock.json').write_text(json.dumps(lock,indent=2)+'\n')
 print(json.dumps({'archive':str(out),'bytes':out.stat().st_size,'files':len(rows),'sha256':lock['archive']['sha256']}))
if __name__=='__main__':main()
