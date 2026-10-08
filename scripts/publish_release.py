#!/usr/bin/env python3
"""Prepare a production channel only after recorded company acceptance; never pushes or resets tags."""
import argparse,json,re,subprocess
from pathlib import Path
B=Path(__file__).resolve().parents[1]
REQUIRED=['company_native_start','company_backend','company_provider_keys','openai_vision','person_product','deepseek','elevenlabs','golden_e2e','subtitle','cta','full_decode','restart_data','update_drill','rollback_drill']
def main():
 p=argparse.ArgumentParser();p.add_argument('--acceptance',type=Path,required=True);p.add_argument('--tag',required=True);a=p.parse_args()
 if not re.fullmatch(r'v\d+\.\d+\.\d+',a.tag):raise ValueError('Production tag must be vX.Y.Z')
 if subprocess.run(['git','rev-parse','-q','--verify','refs/tags/'+a.tag],cwd=B,stdout=subprocess.DEVNULL).returncode==0:raise ValueError('TAG_EXISTS: never reset a release tag')
 evidence=json.loads(a.acceptance.read_text());missing=[x for x in REQUIRED if evidence.get(x)!='PASS']
 if missing:raise ValueError('ACCEPTANCE_PENDING: '+', '.join(missing))
 version=(B/'AUTOCUT_VERSION').read_text().strip();build=json.loads((B/'manifests/latest-build.json').read_text())
 if a.tag!='v'+version or build['version']!=version:raise ValueError('VERSION_MISMATCH')
 if evidence.get('build_id')!=build['build_id']:raise ValueError('BUILD_ACCEPTANCE_MISMATCH')
 (B/'manifests/production.json').write_text(json.dumps({'approved':True,'tag':a.tag,'version':version,'build_id':build['build_id'],'schema_version':build['schema_version'],'acceptance':{k:evidence[k] for k in REQUIRED}},indent=2)+'\n')
 print('Production channel prepared. Review, commit, create the new tag and push; no existing tag was modified.')
if __name__=='__main__':main()
