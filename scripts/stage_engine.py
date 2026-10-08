#!/usr/bin/env python3
"""Stage the pinned Engine snapshot, never read or mutate a developer's mother tree."""
import hashlib,json,os,shutil
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
SOURCE=BASE/'vendor/engine'; DEST=BASE/'staging/engine'
def sha(path):
 with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def stage():
 manifest=json.loads((BASE/'manifests/engine-snapshot.json').read_text())
 for row in manifest['files']:
  if sha(SOURCE/row['path'])!=row['source_sha256']:raise ValueError('ENGINE_SNAPSHOT_CHANGED: '+row['path'])
 # Always start clean: compatibility.py is intentionally applied exactly once.
 if DEST.exists():shutil.rmtree(DEST)
 shutil.copytree(SOURCE,DEST)
 shutil.copytree(BASE/'resources/cta',DEST/'assets/cta')
 shutil.copytree(BASE/'resources/fonts',DEST/'assets/fonts')
 font=DEST/'assets/fonts/Mathilde-BlackItalic.ttf'
 if not font.exists():
  licensed=Path(os.environ.get('CF_LICENSED_FONT',BASE/'local-resources/Mathilde-BlackItalic.ttf'))
  if not licensed.is_file():raise ValueError('LICENSED_MATHILDE_REQUIRED: supply CF_LICENSED_FONT; no font substitution is allowed')
  if sha(licensed)!='3dbb44f22e330eac8349d30e7bdd951a20df53c4049a2a46630f03d0171e1255':raise ValueError('MATHILDE_HASH_MISMATCH')
  shutil.copy2(licensed,font)
 model=BASE/'staging/models/ggml-base.bin'
 if not model.is_file():raise ValueError('RUNTIME_NOT_PREPARED: run scripts/setup_company_mac.sh')
 (DEST/'models').mkdir();shutil.copy2(model,DEST/'models/ggml-base.bin')
 p=DEST/'presets/tiktok_br_transport_v1.json';cfg=json.loads(p.read_text());cfg['subtitle']['font_file']='assets/fonts/Poppins-Bold.ttf';p.write_text(json.dumps(cfg,ensure_ascii=False,indent=2))
 p=DEST/'app/s3/headless_media.py';s=p.read_text().replace("DATA = Path.home() / 'Library/Application Support/Creative Factory'","DATA = Path(__import__('os').environ['AUTOCUT_ENGINE_DATA'])")
 p.write_text(s.replace("review.get('reviewer','').casefold()!='codex'","review.get('review_contract')!='AUTOCUT_REVIEW_V1'"))
 (BASE/'manifests/engine-copy.json').write_text(json.dumps(manifest['files'],indent=2))
 print('Engine snapshot verified:',manifest['engine_fingerprint'])
if __name__=='__main__':stage()
