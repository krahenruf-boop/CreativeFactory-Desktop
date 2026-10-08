"""Local whisper.cpp v1.9.4, commit 927cfce34f31707e17f2bff35c349632fb9e2c3a.
MIT notice: vendor/whisper.cpp/LICENSE. No model is downloaded on import/startup.
CLI JSON contract: upstream examples/cli/cli.cpp output_json; offsets in ms.
Model metadata: https://github.com/ggml-org/whisper.cpp/blob/v1.9.4/models/README.md
"""
import array,hashlib,json,subprocess,wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from ..media import FFMPEG,source_state
from .engine import uid,stamp,digest

MODEL={'id':'whisper-base-multilingual','name':'Whisper base 多语言（ggml）','bytes':147951465,'sha1':'465707469ff3a37a2b9b8d8f89f2f99de7299dac','url':'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin'}
def filehash(p,algorithm='sha256'):
    h=hashlib.new(algorithm)
    with Path(p).open('rb') as f:
        while chunk:=f.read(1024*1024):h.update(chunk)
    return h.hexdigest()

def classify(raw,duration):
    segments=[]
    for item in raw.get('transcription',[]):
        text=item.get('text','').strip();start=item['offsets']['from']/1000;end=item['offsets']['to']/1000
        if not 0<=start<=end<=duration+1:raise ValueError('识别时间信息超出音频范围')
        if text:segments.append(dict(start=start,end=end,text=text))
    text=' '.join(s['text'] for s in segments)
    # ASR alone cannot certify sales speech versus song/noise/hallucination.
    return dict(text=text,segments=segments,language=raw.get('result',{}).get('language','unknown'),speech_state='REVIEW',reason='自动识别候选，需复核是否为有效口播；歌曲/噪音/短句不自动当作文案' if text else '音频非静音，但未识别出可靠文本，请人工复核')

class LocalASR:
    def __init__(self,engine,pool,root):self.e=engine;self.s=engine.store;self.scripts=pool;self.root=Path(root);self.pool=ThreadPoolExecutor(max_workers=1)
    @property
    def binary(self):return self.root/'.tools/whisper.cpp/build/bin/whisper-cli'
    def config(self):return self.s.get('s2_asr_config','local',dict(id='local',model_path=str(self.e.data/'asr/ggml-base.bin')))
    def status(self):
        c=self.config();p=Path(c['model_path'])
        return dict(**c,model=dict(MODEL,destination=str(self.e.data/'asr/ggml-base.bin')),executable_ready=self.binary.is_file(),model_ready=p.is_file(),download=self.s.get('s2_asr_job','download'),jobs=[j for j in self.s.all('s2_asr_job') if j['id']!='download'])
    def configure(self,path):
        p=Path(path).expanduser()
        if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.suffix!='.bin':raise ValueError('请填写已有 ggml .bin 模型的完整路径；不扫描其他目录')
        if p.stat().st_size<1000000:raise ValueError('模型文件不完整')
        return self.s.put('s2_asr_config',dict(id='local',model_path=str(p.resolve())))
    def download(self,confirmed):
        if not confirmed:raise ValueError('请先确认模型名称、147951465 字节下载量和保存路径')
        with self.e.lock:
            old=self.s.get('s2_asr_job','download')
            if old and old['state'] in ['QUEUED','RUNNING']:return old
            target=self.e.data/'asr/ggml-base.bin'
            if target.exists():raise ValueError('目标模型已存在，请直接使用或配置已有模型')
            j=self.s.put('s2_asr_job',dict(id='download',state='QUEUED',bytes=0,total_bytes=MODEL['bytes'],created_at=stamp()))
            self.pool.submit(self.download_worker,target);return j
    def download_worker(self,target):
        temp=target.with_suffix('.partial');j=self.s.get('s2_asr_job','download')
        try:
            j['state']='RUNNING';self.s.put('s2_asr_job',j)
            with httpx.stream('GET',MODEL['url'],follow_redirects=True,timeout=60,trust_env=False) as r:
                r.raise_for_status()
                with temp.open('wb') as f:
                    for part in r.iter_bytes(1024*1024):
                        j['bytes']+=len(part)
                        if j['bytes']>MODEL['bytes']:raise ValueError('模型大小不符')
                        f.write(part);self.s.put('s2_asr_job',j)
            if j['bytes']!=MODEL['bytes'] or filehash(temp,'sha1')!=MODEL['sha1']:raise ValueError('模型完整性校验失败')
            temp.replace(target);self.s.put('s2_asr_config',dict(id='local',model_path=str(target)));j['state']='COMPLETED'
        except Exception:j.update(state='FAILED',error='模型下载或校验失败；未启用不完整文件，可手动重新下载')
        finally:temp.unlink(missing_ok=True);self.s.put('s2_asr_job',j)
    def submit(self,task_id,ids):
        if not 1<=len(ids)<=30:raise ValueError('单次选取 1–30 个已导入素材')
        assets=[self.s.get('asset',id) for id in dict.fromkeys(ids)]
        if any(not a or a['task_id']!=task_id for a in assets):raise ValueError('只处理当前任务所选素材')
        with self.e.lock:
            if any(j['state'] in ['QUEUED','RUNNING'] for j in self.s.all('s2_asr_job') if j['id']!='download'):raise ValueError('已有本地识别任务，请等待完成')
            j=self.s.put('s2_asr_job',dict(id=uid(),task_id=task_id,asset_ids=ids,state='QUEUED',items=[],created_at=stamp()))
            self.pool.submit(self.analyze,j,assets);return j
    def analyze(self,j,assets):
        j['state']='RUNNING';self.s.put('s2_asr_job',j)
        for a in assets:
            try:
                result=self.analyze_one(a)
                j['items'].append(dict(asset_id=a['id'],status='PASS',speech_state=result['speech_state'],cache=result.get('cache',False)))
            except Exception as e:j['items'].append(dict(asset_id=a['id'],status='BLOCKED' if isinstance(e,FileNotFoundError) else 'FAIL',error='缺少 ASR 模型，请先确认下载或配置已有模型' if isinstance(e,FileNotFoundError) else '本地识别失败，请检查素材、时长与模型兼容性；可先手动录入'))
            self.s.put('s2_asr_job',j)
        j['state']='COMPLETED';self.s.put('s2_asr_job',j)
    def analyze_one(self,a):
        if source_state(a)!='OK':raise ValueError('素材失效')
        if a['metadata']['duration']>900:raise ValueError('S2 单素材识别上限 15 分钟')
        key=digest([filehash(a['path']),'no-audio' if not a['metadata']['audio'] else filehash(self.config()['model_path']), 'whisper-1.9.4'])
        old=self.s.get('s2_transcript_cache',key)
        if old:r=dict(old,cache=True)
        elif not a['metadata']['audio']:r=dict(text='',segments=[],language='unknown',speech_state='NO_SPEECH',reason='源文件没有音轨')
        else:
            if not self.binary.is_file():raise FileNotFoundError()
            work=self.e.data/'asr'/uid();work.mkdir();wav=work/'audio.wav';out=work/'transcript'
            try:
                subprocess.run([FFMPEG,'-v','error','-nostdin','-i',a['path'],'-vn','-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],capture_output=True,check=True,timeout=180)
                with wave.open(str(wav)) as w:
                    duration=w.getnframes()/w.getframerate();samples=array.array('h',w.readframes(w.getnframes()))
                if not samples or max(map(abs,samples))<8:r=dict(text='',segments=[],language='unknown',speech_state='NO_SPEECH',reason='音轨接近数字静音（非 VAD 口播判断）')
                else:
                    subprocess.run([str(self.binary),'-m',self.config()['model_path'],'-f',str(wav),'-l','auto','-ojf','-of',str(out),'-t','4','-np'],capture_output=True,check=True,timeout=900)
                    raw=json.loads(out.with_suffix('.json').read_text());r=classify(raw,duration)
                    r['raw']=raw
            finally:
                for p in work.iterdir():p.unlink()
                work.rmdir()
        self.s.put('s2_transcript_cache',dict(r,id=key))
        transcript=dict(r,id=digest([key,a['id']]),asset_id=a['id'],task_id=a['task_id'],created_at=stamp(),source_fingerprint=a['fingerprint'])
        self.s.put('s2_transcript',transcript)
        a['speech_state']=r['speech_state'];self.s.put('asset',a)
        if r['text']:self.scripts.add(a['task_id'],r['text'],a['id'],r['speech_state'],transcript['id'])
        return r
    def recover(self):
        for j in self.s.all('s2_asr_job'):
            if j['state'] in ['RUNNING','QUEUED']:j.update(state='INTERRUPTED',error='上次本地处理已中断，请手动重试');self.s.put('s2_asr_job',j)
