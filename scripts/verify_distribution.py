#!/usr/bin/env python3
"""Validate staged Git contents before publication; never prints credential matches."""
import hashlib,json,re,subprocess,sys
from pathlib import Path
B=Path(__file__).resolve().parents[1]
paths=subprocess.check_output(['git','diff','--cached','--name-only','--diff-filter=ACM','-z'],cwd=B).decode().split('\0')
failures=[]
for name in filter(None,paths):
 p=B/name
 if any(x in p.parts for x in ['.venv','__pycache__','cache','user_assets']) or p.suffix.lower() in {'.mp4','.mov','.sqlite3','.db','.pem','.key'}:failures.append(name+': forbidden data')
 if p.name in {'Mathilde-BlackItalic.ttf','.env','auth.json'}:failures.append(name+': excluded resource')
 blob=subprocess.check_output(['git','show',':'+name],cwd=B)
 if len(blob)>50_000_000:failures.append(name+': large ordinary Git object')
 if any(re.search(pattern,blob) for pattern in [rb'-----BEGIN (?:OPENSSH|RSA|EC) PRIVATE KEY-----',rb'gh[pousr]_[A-Za-z0-9]{25,}',rb'sk-(?:proj-)?[A-Za-z0-9_-]{32,}']):failures.append(name+': possible credential')
 if 'ProPainter/weights' in name:failures.append(name+': noncommercial weight')
meta=json.loads((B/'manifests/engine-snapshot.json').read_text())
for r in meta['files']:
 if hashlib.sha256((B/'vendor/engine'/r['path']).read_bytes()).hexdigest()!=r['source_sha256']:failures.append(r['path']+': engine drift')
if failures:print('\n'.join(failures));sys.exit(1)
print('Staged source/data/credential policy and Engine fingerprint PASS')
