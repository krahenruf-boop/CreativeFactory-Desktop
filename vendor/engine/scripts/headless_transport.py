"""Shared Headless execution entry. Explicit stages, no automatic paid or UI work."""
import argparse
import json
import os
import re
from pathlib import Path
import sys
from concurrent.futures import as_completed
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.s3.headless_runtime import Harness,read,write,sha,fingerprint
from app.s3.headless_media import (probe,identity,preflight_job,approve_preflight,final_job,
                                  frame_bank,technical_qa,contact_sheet,batch_sheets)


def item_error(exc,item):
    code=str(exc)
    return dict(status='REVIEW_REQUIRED',item=str(item),error=code if re.fullmatch(r'[A-Z][A-Z0-9_]{1,99}',code) else 'STAGE_INPUT_REVIEW_REQUIRED')


def scan(h,task):
    with h.profiler.span('SCAN'):
        paths=sorted((p for p in task.iterdir() if p.is_file() and not p.name.startswith('.') and
                      p.suffix.lower() in {'.mp4','.mov','.m4v','.webm'} and
                      not any(x in p.stem.lower() for x in ['.partial','.temp'])),key=lambda p:p.name.casefold())
    records=[]
    for rank,p in enumerate(paths,1):
        with h.profiler.span('HASH',rank):source_hash=sha(p)
        try:
            r=h.execute('FFPROBE',rank,{'sha256':source_hash},lambda p=p:dict(metadata=probe(p)))
            info=r['metadata'];v=next(s for s in info['streams'] if s['codec_type']=='video' and not s.get('disposition',{}).get('attached_pic'))
            record=dict(rank=rank,path=str(p),filename=p.name,sha256=source_hash,status='VALID',duration=float(v.get('duration',info['format']['duration'])),width=v['width'],height=v['height'],fps=v['avg_frame_rate'],codec=v['codec_name'],audio=[s for s in info['streams'] if s['codec_type']=='audio'])
        except Exception:record=dict(rank=rank,path=str(p),sha256=source_hash,status='INVALID_SOURCE')
        records.append(record)
    write(h.work/'manifest.json',dict(sources=records));return records


def qa_job(h,plan):
    key=identity(plan);folder=h.work/'qa'/str(plan['rank']);video=Path(plan['output'])
    def call():
        regions=plan.get('interval_composition',{}).get('regions',plan.get('overlays',[]))
        regions=[r for r in regions if r.get('overlay_class')!='PRODUCT_BRANDING']
        if any('output_interval' not in r for r in regions):
            return dict(status='REVIEW_REQUIRED',error='OVERLAY_QA_OUTPUT_INTERVAL_REQUIRED')
        cut_samples=[]
        for segment in plan.get('montage_segments',[]):
            lo,hi=segment['output_start'],segment['output_end']
            cut_samples += [lo,(lo+hi)/2,max(lo,hi-1/30)]
        bank=frame_bank(video,folder,overlay_regions=regions,duration=plan['target_duration'],sample_times=cut_samples)
        evidence=read(h.work/'render'/key/'visual_evidence.json')
        bank=technical_qa(plan,evidence,bank);bank['rank']=plan['rank']
        write(folder/'qa.json',bank)
        bank['artifacts']=[dict(path=str(folder/'qa.json'),sha256=sha(folder/'qa.json'))]
        with h.profiler.span('CONTACT_SHEET',plan['rank']):
            sheet=contact_sheet(bank,folder/'contact.jpg');bank['artifacts'].append(sheet)
        for s in bank['samples']:bank['artifacts'].append(dict(path=s['path'],sha256=sha(s['path'])))
        return bank
    return h.submit('TECH_QA',plan['rank'],dict(plan=key,output_sha256=sha(video)),call)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('stage',choices=['scan','asr','deepseek','approve-titles','tts','preflight','approve-preflight','render','qa','batch-sheets','report'])
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--input',type=Path)
    parser.add_argument('--execute',action='store_true');parser.add_argument('--model',type=Path)
    parser.add_argument('--catalog',type=Path);parser.add_argument('--existing-results',type=Path)
    parser.add_argument('--review',type=Path)
    a=parser.parse_args();work=a.work.resolve();work.mkdir(parents=True,exist_ok=True)
    with Harness(work) as h:
        if a.stage=='scan':return scan(h,work.parent)
        if a.stage=='report':return {'status':'PASS'}
        if a.stage=='asr':
            from app.s3.headless_jobs import asr_job
            if not a.model:parser.error('asr requires existing --model')
            # Read only this task's transcript headers once; never search Downloads.
            prior={}
            for p in (work/'transcripts').glob('*.json'):
                r=read(p)
                if r.get('source_sha256'):prior[r['source_sha256']]=p
            futures=[];errors=[]
            for s in read(work/'manifest.json')['sources']:
                if s['status']!='VALID':continue
                try:futures.append(asr_job(h,s,a.model,existing_transcript=s.get('transcript') or prior.get(s['sha256'])))
                except Exception as exc:errors.append(item_error(exc,s['rank']))
            return errors+[f.result() for f in futures]
        if not a.input:parser.error('stage requires --input JSON')
        items=read(a.input)
        if a.stage=='deepseek':
            if not a.execute:parser.error('DeepSeek requires explicit --execute')
            from app.s3.deepseek_batch import generate_batch
            return generate_batch(items,work)
        if a.stage=='approve-titles':
            from app.s3.deepseek_batch import approve_title
            if not a.review:parser.error('requires --review JSON with user confirmation and Codex checks')
            with h.profiler.span('TITLE_REVIEW',items['script_group_id']):return approve_title(items,read(a.review),work)
        if a.stage=='tts':
            from app.s3.headless_jobs import tts_job
            from app.s2.engine import Engine
            from app.s2.providers import Official
            from app.s2.types import TTS
            if not a.catalog:parser.error('tts requires verified current --catalog JSON')
            cat=read(a.catalog);existing=read(a.existing_results,[]) if a.existing_results else [];attempts=[]
            db=Path.home()/'Library/Application Support/Creative Factory/creative_factory.sqlite3'
            if db.exists():
                import sqlite3
                with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as conn:
                    for kind in ('s2_result','s2_attempt'):
                        rows=[json.loads(row[0]) for row in conn.execute('SELECT data FROM records WHERE kind=?',(kind,))]
                        scoped=[r for r in rows if r.get('scope')==cat['scope']]
                        if kind=='s2_result':existing.extend(r for r in scoped if r.get('kind')=='tts')
                        else:attempts.extend(scoped)
            def provider():
                key=os.environ.get('ELEVENLABS_API_KEY','')
                if not key:raise ValueError('ELEVENLABS_NOT_CONFIGURED')
                return Official('elevenlabs',key)
            futures=[];errors=[]
            for index,item in enumerate(items):
                # Existing schema/catalog validation, no catalog HTTP refresh.
                try:
                    payload,_=Engine.tts_payload(None,TTS.model_validate(item['payload']),cat)
                    futures.append(tts_job(h,payload,scope=cat['scope'],provider_factory=provider,
                        review=item['review'],existing_results=existing,existing_attempts=attempts,execute=a.execute))
                except Exception as exc:errors.append(item_error(exc,index))
            return errors+[f.result() for f in futures]
        if a.stage=='batch-sheets':
            with h.profiler.span('CONTACT_SHEET','batch'):
                return batch_sheets([read(p) for p in items['qa_files']],items.get('overlays',[]),work/'contact_sheets')
        plans=items if isinstance(items,list) else [items]
        if a.stage=='approve-preflight':
            if len(plans)!=1 or not a.review:parser.error('one plan and --review are required')
            with h.profiler.span('VISUAL_QA',plans[0]['rank']):approve_preflight(h,plans[0],read(a.review))
            return {'status':'PASS'}
        results=[];futures=[];qa_futures=[]
        for p in plans:
            try:
                if a.stage=='preflight':futures.append((p,preflight_job(h,p)))
                elif a.stage=='render':
                    if not a.execute:parser.error('final render requires --execute and saved preflight approval')
                    futures.append((p,final_job(h,p)))
                elif a.stage=='qa':qa_futures.append(qa_job(h,p))
            except Exception as exc:results.append(item_error(exc,p.get('rank','unknown')))
        mapping={f:p for p,f in futures}
        for f in as_completed(mapping):
            result=f.result();results.append(result)
            if a.stage=='render' and result.get('output'):
                try:qa_futures.append(qa_job(h,mapping[f]))
                except Exception as exc:results.append(item_error(exc,mapping[f]['rank']))
        results.extend(f.result() for f in qa_futures)
        if a.stage in {'render','qa'}:
            banks=[r for r in results if r.get('samples')]
            regions=[]
            for p in plans:
                for i,r in enumerate(p.get('interval_composition',{}).get('regions',p.get('overlays',[]))):
                    if r.get('overlay_class')=='PRODUCT_BRANDING':continue
                    e=dict(id=f'{p["rank"]}:{r.get("id",i)}',status='PENDING')
                    directory=Path(r['mask_path']).parent if r.get('mask_path') else None
                    for k in ('before_crop','after_crop','mask_preview'):
                        path=Path(r[k]) if r.get(k) else directory/(k+'.png') if directory else None
                        if path and path.is_file():e[k]=str(path)
                    if not all(k in e for k in ('before_crop','after_crop','mask_preview')):e['status']='EVIDENCE_MISSING'
                    regions.append(e)
            if banks:
                with h.profiler.span('CONTACT_SHEET','batch'):
                    batch_sheets(banks,regions,work/'contact_sheets')
        return results


if __name__=='__main__':
    result=main()
    # Stage artifacts retain detail; CLI avoids scripts, credentials and verbose metadata.
    values=result if isinstance(result,list) else [result]
    print(json.dumps([{'status':v.get('status','RECORDED'),'error':v.get('error'),'cache_hit':v.get('cache_hit')} for v in values],ensure_ascii=False))
