import os,json,shutil,sys
from pathlib import Path
RES=Path(os.environ.get('AUTOCUT_RESOURCES',Path(__file__).resolve().parents[2]/'staging')).resolve()
HOME=Path(os.environ.get('AUTOCUT_DATA_HOME',Path.home()/'Library/Application Support/AutoCut')).resolve()
ENGINE=RES/'engine'
os.environ['AUTOCUT_ENGINE_DATA']=str(HOME/'user_assets')
os.environ['PATH']=str(RES/'runtime/media/bin')+':'+os.environ.get('PATH','/usr/bin:/bin')
sys.path.insert(0,str(ENGINE))
def asset():
 row=json.loads((ENGINE/'assets/cta/current.json').read_text());src=ENGINE/'assets/cta'/row['file_path'];dst=HOME/'user_assets/cta_assets'/src.name;dst.parent.mkdir(parents=True,exist_ok=True)
 if not dst.exists():shutil.copy2(src,dst)
 from app.s3.headless_runtime import sha
 if sha(dst)!=row['sha256']:raise ValueError('CTA_ASSET_CHANGED')
 row['file_path']=str(dst);return row
