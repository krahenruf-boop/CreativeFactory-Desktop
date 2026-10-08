"""Concrete ASR/TTS jobs for shared queues; no browser or server dependency."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import re
import copy
from .headless_runtime import ROOT, fingerprint, read, sha, write
from .headless_media import probe
from app.s2.asr import classify
from app.s2.tts_text import prepare_tts_text, tts_request_payload


def alignment_valid(data,text,duration):
    a=data.get('alignment') or data.get('normalized_alignment') or {}
    starts=a.get('character_start_times_seconds',[]);ends=a.get('character_end_times_seconds',[])
    return (''.join(a.get('characters',[]))==text and len(starts)==len(ends)==len(a.get('characters',[]))
            and bool(starts) and all(0<=s<=e<=duration+.15 for s,e in zip(starts,ends))
            and all(x<=y for x,y in zip(starts,starts[1:])) and all(x<=y for x,y in zip(ends,ends[1:])))


def asr_job(harness, source, model, *, binary=None, existing_transcript=None):
    source=copy.deepcopy(source)
    binary=Path(binary or ROOT/'.tools/whisper.cpp/build/bin/whisper-cli')
    with harness.profiler.span('HASH',source['sha256']):
        if sha(source['path'])!=source['sha256']: raise ValueError('SOURCE_HASH_CHANGED')
        model_hash=harness.dependency_hash(model)
    inputs=dict(source_sha256=source['sha256'],model_sha256=model_hash,binary_sha256=harness.dependency_hash(binary),language='auto')
    def call():
        if sha(source['path'])!=source['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
        if existing_transcript:
            old=read(existing_transcript)
            if old.get('source_sha256')==source['sha256'] and old.get('model_sha256')==model_hash:
                return old | dict(artifacts=[dict(path=str(existing_transcript),sha256=sha(existing_transcript))],prior_cache_hit=True)
        if not source.get('audio'):
            result=dict(status='REVIEW',speech_state='NO_SPEECH',text='',segments=[],language='unknown',**inputs)
            path=harness.work/'transcripts'/(fingerprint(inputs)+'.json');write(path,result)
            return result|dict(transcript=str(path),artifacts=[dict(path=str(path),sha256=sha(path))])
        with tempfile.TemporaryDirectory(prefix='asr-',dir=harness.work/'temp') as temp:
            wav=Path(temp)/'audio.wav';target=Path(temp)/'transcript'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',source['path'],'-vn','-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],capture_output=True,check=True,timeout=180)
            subprocess.run([str(binary),'-m',str(model),'-f',str(wav),'-l','auto','-ojf','-of',str(target),'-t','4','-np'],capture_output=True,check=True,timeout=900)
            result=classify(read(target.with_suffix('.json')),source['duration'])
        path=harness.work/'transcripts'/(fingerprint(inputs)+'.json');result.update(inputs);write(path,result)
        return dict(status='REVIEW',transcript=str(path),artifacts=[dict(path=str(path),sha256=sha(path))])
    (harness.work/'temp').mkdir(parents=True,exist_ok=True)
    return harness.submit('ASR',source['sha256'],inputs,call)


def tts_job(harness, payload, *, scope, provider_factory, review, existing_results=(), existing_attempts=(),execute=False):
    """Validate payload with Engine.tts_payload first. Reuse s2-v1 cache identity.

    existing_results: current account's read-only s2_result/prior headless records.
    provider_factory: existing Official ElevenLabs provider, no replacement API.
    """
    p=copy.deepcopy(payload);p['tts_text']=prepare_tts_text(p['text'])
    key=fingerprint(dict(provider='elevenlabs',scope=scope,kind='tts',payload=tts_request_payload(p),schema='s2-v1',generation=None))
    if not scope or review.get('title_status')!='USER_APPROVED' or review.get('script_status')!='CODEX_APPROVED' or review.get('tts_text_sha256')!=fingerprint(p['tts_text']):
        raise ValueError('TITLE_AND_SCRIPT_APPROVAL_REQUIRED')
    candidates=[r for r in existing_results if r.get('fingerprint')==key and r.get('scope')==scope]
    def call():
        folder=harness.work/'audio';folder.mkdir(parents=True,exist_ok=True)
        mp3=folder/(key+'.mp3');alignment_path=folder/(key+'.alignment.json');record=folder/(key+'.json')
        for r in candidates:
            if Path(r['path']).is_file() and sha(r['path'])==r['sha256'] and Path(r['alignment_path']).is_file():
                if not alignment_valid(read(r['alignment_path']),p['tts_text'],r['duration']):
                    return dict(status='REVIEW_REQUIRED',error='EXISTING_TTS_ALIGNMENT_INVALID')
                for src,dst in [(r['path'],mp3),(r['alignment_path'],alignment_path)]:
                    if Path(src).resolve()!=dst.resolve(): shutil.copy2(src,dst)
                result=dict(r,path=str(mp3),alignment_path=str(alignment_path),new_requests=0,prior_cache_hit=True)
                result['artifacts']=[dict(path=str(x),sha256=sha(x)) for x in (mp3,alignment_path)]
                write(record,result);return result
        if candidates:return dict(status='REVIEW_REQUIRED',error='EXISTING_TTS_ARTIFACT_INVALID')
        if any(a.get('fingerprint')==key and (a.get('status') in {'QUEUED','RUNNING','SUCCEEDED'}
                or (a.get('cost_state')=='BILLING_UNCERTAIN' and not a.get('reconciled'))) for a in existing_attempts):
            return dict(status='REVIEW_REQUIRED',error='EXISTING_TTS_ATTEMPT_REQUIRES_RECONCILIATION')
        if not execute: return dict(status='REVIEW_REQUIRED',error='EXPLICIT_TTS_EXECUTION_REQUIRED')
        provider=provider_factory()
        try: response=provider.tts(p)
        finally:
            if hasattr(provider,'client'): provider.client.close()
        # Keep a successful charged response before QA: visual/technical repairs never repeat TTS.
        mp3.write_bytes(response['audio'])
        write(alignment_path,{k:response.get(k) for k in ('alignment','normalized_alignment')})
        result=dict(status='REVIEW',fingerprint=key,scope=scope,payload=p,path=str(mp3),sha256=sha(mp3),alignment_path=str(alignment_path),
                    usage=response.get('usage'),request_id=response.get('request_id'),new_requests=1,audio_review={'status':'REVIEW'},
                    artifacts=[dict(path=str(x),sha256=sha(x)) for x in (mp3,alignment_path)])
        write(record,result)
        with harness.profiler.span('ALIGNMENT',key):
            info=probe(mp3);duration=float(info['format']['duration']);result['duration']=duration
            valid=alignment_valid(response,p['tts_text'],duration)
            cp=subprocess.run(['ffmpeg','-v','info','-xerror','-i',str(mp3),'-af','volumedetect','-f','null','-'],capture_output=True,text=True,timeout=90)
            volume=re.findall(r'mean_volume: ([\-\w.]+) dB',cp.stderr)
            result['technical_status']='PASS' if valid and duration>0 and cp.returncode==0 and volume and float(volume[-1])>-55 else 'REVIEW_REQUIRED'
        write(record,result);return result
    return harness.submit('TTS',key,dict(fingerprint=key),call,paid=execute)
