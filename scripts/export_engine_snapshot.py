#!/usr/bin/env python3
"""Explicit maintainer export of the mature Engine import closure, never a build-time dependency."""
import argparse,ast,hashlib,json,shutil,subprocess
from pathlib import Path
B=Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);args=parser.parse_args();source=args.source.resolve(strict=True)
 pending=['app.s3.headless_runtime','app.s3.headless_jobs','app.s3.headless_media','app.s3.visual_preset','app.s3.overlay_policy','app.s3.experimental_propainter','app.s3.deepseek_batch','app.s3.deepseek_copy','scripts.headless_transport','scripts.headless_overlay','scripts.headless_overlay_policy'];seen={}
 while pending:
  mod=pending.pop();p=source/(mod.replace('.','/')+'.py')
  if mod in seen or not p.exists():continue
  seen[mod]=p
  for n in ast.walk(ast.parse(p.read_text())):
   names=[]
   if isinstance(n,ast.Import):names=[x.name for x in n.names]
   elif isinstance(n,ast.ImportFrom):
    parent=mod.split('.')[:-n.level] if n.level else [];root='.'.join(parent+([n.module] if n.module else []));names=[root]+[root+'.'+x.name for x in n.names]
   pending.extend(x for x in names if x.startswith(('app.','scripts.')))
 paths=list(seen.values())
 for folder in ['prompts','presets','docs']:
  paths.extend(p for p in (source/folder).iterdir() if p.is_file() and (folder!='docs' or p.name.startswith('HEADLESS_') and p.suffix=='.md'))
 target=B/'vendor/engine';new=B/'build/engine-snapshot-new'
 if new.exists():raise ValueError('Snapshot staging exists; inspect it before another export')
 rows=[]
 for p in paths:
  rel=p.relative_to(source);out=new/rel;out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,out);rows.append(dict(path=rel.as_posix(),source_sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
 old=B/'build/engine-snapshot-previous'
 if old.exists():raise ValueError('Previous snapshot backup exists; preserve/review it first')
 if target.exists():target.rename(old)
 new.rename(target)
 meta=dict(engine_commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip(),engine_fingerprint=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),snapshot_type='exact_headless_dependency_closure',upstream='CreativeFactory',schema_version=3,files=rows)
 (B/'manifests/engine-snapshot.json').write_text(json.dumps(meta,indent=2)+'\n');print(meta['engine_fingerprint'])
if __name__=='__main__':main()
