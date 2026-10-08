import hashlib,json,os,sqlite3,threading,time,uuid,subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .types import ProviderConfig,Batch,Confirm,Localize
from .providers import Official,FakeProvider,ProviderError,localize_messages
from .tts_text import prepare_tts_text,tts_request_payload
from ..media import FFPROBE,FFMPEG

ACTIVE={'QUEUED','RUNNING'}
HELD=ACTIVE|{'BILLING_UNCERTAIN','SUCCEEDED','INVALID_AUDIO','INVALID_LOCALIZATION'}
def uid():return uuid.uuid4().hex
def stamp():return datetime.now(timezone.utc).isoformat()
def digest(obj):return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class Store:
    def __init__(self,db):self.db=db
    def connect(self):return sqlite3.connect(self.db,timeout=20)
    def all(self,kind):
        with self.connect() as c:return [json.loads(r[0]) for r in c.execute('SELECT data FROM records WHERE kind=? ORDER BY rowid DESC',(kind,))]
    def get(self,kind,id,default=None):
        with self.connect() as c:r=c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,id)).fetchone()
        return json.loads(r[0]) if r else default
    def put(self,kind,data):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)',(kind,data['id'],json.dumps(data,ensure_ascii=False)))
        return data
    def delete(self,kind,id):
        with self.connect() as c:c.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,id))

class Engine:
    def __init__(self,db,data,factory=None):
        self.store=Store(db);self.data=Path(data);self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=1)
        self.factory=factory or (lambda p,k:FakeProvider() if p=='fake' else Official(p,k))
        for name in ['audio','config','asr']:(self.data/name).mkdir(parents=True,exist_ok=True)
        os.chmod(self.data/'config',0o700)
        self.secret_path=self.data/'config/credentials.json'
    def recover(self):
        with self.lock:
            for a in self.store.all('s2_attempt'):
                if a['status']=='QUEUED':a.update(status='CANCELLED',cost_state='UNKNOWN',reserved_amount=0,ended_at=stamp())
                elif a['status']=='RUNNING':a.update(status='BILLING_UNCERTAIN',cost_state='BILLING_UNCERTAIN',ended_at=stamp(),error='进程中断，需核对供应商记录')
                else:continue
                self.store.put('s2_attempt',a)
    def config(self,p):
        c=self.store.get('s2_config',p)
        if c:return c
        spec=ProviderConfig(provider=p,enabled=p=='fake',account_scope='local-fixture' if p=='fake' else '')
        return dict(id=p,revision='initial',**spec.model_dump(exclude={'key','clear_key'}))
    def secrets(self):
        if self.secret_path.is_symlink():raise ValueError('凭据路径不能是符号链接')
        if not self.secret_path.exists():return {}
        return json.loads(self.secret_path.read_text())
    def settings(self):
        keys=self.secrets()
        return {'providers':[dict(self.config(p),key_configured=bool(keys.get(p)),catalog_ready=bool(self.catalog(p,False))) for p in ['elevenlabs','deepseek','fake']], 'credentials_path':str(self.secret_path),'storage':'本机权限 0600 文件，未加密；仅后端读取','audio_dir':str(self.data/'audio'),'gemini':{'enabled':False,'status':'仅预留，S2 不调用'}}
    def set_config(self,spec):
        with self.lock:
            try:ZoneInfo(spec.budget.timezone)
            except Exception:raise ValueError('预算时区无效，请使用 Asia/Shanghai 等时区名称') from None
            if spec.enabled and spec.provider!='fake' and not spec.account_scope.strip():raise ValueError('请填写账号/工作区域；切换账号必须使用不同标识')
            keys=self.secrets()
            if spec.clear_key:keys.pop(spec.provider,None)
            elif spec.key and spec.key.get_secret_value().strip():keys[spec.provider]=spec.key.get_secret_value().strip()
            temp=self.secret_path.with_suffix('.tmp')
            if temp.is_symlink():raise ValueError('凭据路径不安全')
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:json.dump(keys,f)
            os.chmod(temp,0o600);os.replace(temp,self.secret_path)
            self.store.put('s2_config',dict(id=spec.provider,revision=uid(),**spec.model_dump(exclude={'key','clear_key'})))
            return self.settings()
    def provider(self,p):
        c=self.config(p)
        if not c['enabled']:raise ValueError('Provider 未启用，请在设置中配置')
        key=self.secrets().get(p,'')
        if p!='fake' and not key:raise ValueError('CONFIG_MISSING：请在本机设置 API Key')
        return self.factory(p,key)
    def catalog(self,p,required=True):
        c=self.config(p)
        if p=='fake':return dict(self.factory(p,'').catalog(),scope=digest([p,c['account_scope'],c['permission_scope']]),revision=c['revision'])
        cat=self.store.get('s2_catalog',p)
        if cat and cat['revision']==c['revision']:return cat
        if required:raise ValueError('请先在设置中检查连接并读取模型/声音；此操作不生成内容')
        return None
    def connect_provider(self,p):
        with self.lock:
            c=self.config(p);provider=self.provider(p)
            try:cat=provider.catalog()
            except ProviderError:
                if p=='elevenlabs':self.store.delete('s2_catalog',p)
                raise
            identity=cat.pop('identity',c['account_scope'])
            if p=='elevenlabs':
                previous=self.store.get('s2_identity',p)
                identity_hash=digest(identity)
                changed=bool(previous and previous['user_id_hash']!=identity_hash)
                self.store.put('s2_identity',dict(id=p,user_id_hash=identity_hash))
                cat.update(account_changed=changed,warning='检测到ElevenLabs账号变化；已重新读取当前账号目录，旧 Voice 缓存不再复用' if changed else '')
            cat.update(id=p,revision=c['revision'],scope=digest([p,identity,c['account_scope'],c['permission_scope']]),checked_at=stamp())
            self.store.put('s2_catalog',cat)
            return cat
    def valid_result(self,id):
        r=self.store.get('s2_result',id)
        if not r:return None
        if r['kind']=='localize':return r
        p=self.data/'audio'/f'{id}.mp3'
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=r.get('sha256'):return None
        return r
    def cached(self,key):
        rows=[a for a in self.store.all('s2_attempt') if a['fingerprint']==key]
        for a in rows:
            if a['status']=='SUCCEEDED' and self.valid_result(a.get('result_id')):return 'HIT',a
        for a in rows:
            if a['status'] in ACTIVE:return 'IN_FLIGHT',a
            if a['cost_state']=='BILLING_UNCERTAIN' and not a.get('reconciled'):return 'BILLING_UNCERTAIN',a
        return 'NEW',None
    def tts_payload(self,item,cat):
        d=item.model_dump() if hasattr(item,'model_dump') else dict(item)
        d['tts_text']=prepare_tts_text(d['text'])
        if not d['tts_text']:raise ValueError('清洗后没有可朗读文本，请检查原稿')
        model=next((m for m in cat['models'] if m['id']==d['model_id']),None)
        voice=next((v for v in cat['voices'] if v['id']==d['voice_id']),None)
        if not model or 'supported' not in model or not voice:raise ValueError('请使用当前账号目录中的声音和 TTS 模型')
        if len(d['tts_text'])>model['max_chars']:raise ValueError('超过所选模型的单次文本上限')
        if d['language']:raise ValueError('S2 不发送 language_code：模型支持不一致，请由文本确定语言')
        for k,v in d['settings'].items():
            if not model['supported'].get(k):raise ValueError(f'UNSUPPORTED_SETTING：模型不支持 {k}')
            if k=='use_speaker_boost':
                if type(v) is not bool:raise ValueError('speaker boost 必须是开关')
            elif type(v) is bool or not (.7<=v<=1.2 if k=='speed' else 0<=v<=1):raise ValueError('声音参数超出范围')
            if k=='stability' and model.get('stability_choices') and v not in model['stability_choices']:raise ValueError('该模型 stability 仅支持 0 / 0.5 / 1')
        # All supported settings are explicit to avoid hidden global voice settings.
        defaults={'speed':1.,'stability':.5,'similarity_boost':.75,'style':0.,'use_speaker_boost':True}
        d['settings']={k:d['settings'].get(k,v) for k,v in defaults.items() if model['supported'].get(k)}
        return d,voice['name']
    def quote_tts(self,batch):
        with self.lock:
            cat=self.catalog(batch.provider);self.provider(batch.provider)
            items=[]
            if batch.task_id and not self.store.get('task',batch.task_id):raise ValueError('任务不存在')
            for item in batch.items:
                d,name=self.tts_payload(item,cat)
                if d.get('script_id'):
                    s=self.store.get('s2_script',d['script_id'])
                    if not s or s['revision']!=d['revision'] or not s.get('confirmed') or s['localized_text']!=d['text']:raise ValueError('文案版本未确认或已变更，请重新读取')
                items.append(dict(payload=d,voice_name=name))
            return self.make_quote(batch.provider,'tts',items,batch.task_id,batch.regenerate)
    def quote_localize(self,spec):
        with self.lock:
            cat=self.catalog(spec.provider);self.provider(spec.provider)
            if spec.model_id not in [m['id'] for m in cat['models']] or (spec.provider=='fake' and spec.model_id!='fake-text'):raise ValueError('请选择文本模型')
            script=self.store.get('s2_script',spec.script_id)
            if not script:raise ValueError('文案不存在')
            d=spec.model_dump();d.update(text=script['text'],revision=script['revision'])
            return self.make_quote(spec.provider,'localize',[dict(payload=d)],script['task_id'],False)
    def estimate(self,c,kind,d,usage=None):
        r=c['rate']
        if r['model_id']!=d['model_id']:return None
        if kind=='tts':
            rate=r['tts_per_1000_chars'];return None if rate is None else len(d.get('tts_text',d['text']))*rate/1000
        rates=[r['input_per_million'],r['cached_input_per_million'],r['output_per_million']]
        if any(v is None for v in rates):return None
        if usage is None:
            n=len(json.dumps(localize_messages(d),ensure_ascii=False).encode())+128
            return (n*max(rates[:2])+2048*rates[2])/1000000
        prompt=usage.get('prompt_tokens');out=usage.get('completion_tokens')
        if prompt is None or out is None:return None
        hit=usage.get('prompt_cache_hit_tokens') or 0
        return ((prompt-hit)*rates[0]+hit*rates[1]+out*rates[2])/1000000
    def make_quote(self,p,kind,items,task_id,regenerate):
        c=self.config(p);cat=self.catalog(p);seen=set();unique=[];generation=uid() if regenerate else None
        for item in items:
            d=item['payload'];payload={k:v for k,v in d.items() if k not in ['script_id','revision','provider']}
            if kind=='tts':payload=tts_request_payload(d)
            key=digest(dict(provider=p,scope=cat['scope'],kind=kind,payload=payload,schema='s2-v1',generation=generation))
            item.update(fingerprint=key,characters=len(payload['text']))
            if key in seen:continue
            seen.add(key);state,a=self.cached(key)
            item.update(cache=state,attempt_id=a['id'] if a else None,estimated_amount=self.estimate(c,kind,d))
            unique.append(item)
        new=[i for i in unique if i['cache']=='NEW']
        q=dict(id=uid(),provider=p,scope=cat['scope'],config_revision=c['revision'],kind=kind,task_id=task_id,items=unique,bindings=items,new_requests=len(new),characters=sum(i['characters'] for i in new),currency=c['rate']['currency'],estimated_amount=sum(i['estimated_amount'] for i in new) if all(i['estimated_amount'] is not None for i in new) else None,created_at=stamp(),created_epoch=time.time(),state='PREVIEW',regenerate=regenerate)
        self.store.put('s2_quote',q);return q
    def today(self,c,timestamp=None):return datetime.fromisoformat(timestamp).astimezone(ZoneInfo(c['budget']['timezone'])).date() if timestamp else datetime.now(ZoneInfo(c['budget']['timezone'])).date()
    def check_budget(self,q,new):
        c=self.config(q['provider']);b=c['budget'];chars=sum(i['characters'] for i in new)
        if any(i['characters']>b['request_chars'] for i in new) or chars>b['batch_chars']:raise ValueError('超过单次/批次字符预算')
        attempts=[a for a in self.store.all('s2_attempt') if a['provider']==q['provider'] and a['status'] in HELD and self.today(c,a['started_at'])==self.today(c)]
        if sum(a['characters'] for a in attempts)+chars>b['daily_chars'] or len(attempts)+len(new)>b['daily_requests']:raise ValueError('超过本应用当日字符/请求预算（含预占）')
        money=sum(i['estimated_amount'] or 0 for i in new)
        unknown=any(i['estimated_amount'] is None for i in new)
        if unknown and any(b[k] is not None for k in ['request_amount','batch_amount','daily_amount']):raise ValueError('费用未知，无法执行金额硬预算；请先核对费率，或仅使用请求/字符上限')
        if b['request_amount'] is not None and any(i['estimated_amount']>b['request_amount'] for i in new):raise ValueError('超过单次金额预算')
        if b['batch_amount'] is not None and money>b['batch_amount']:raise ValueError('超过批次金额预算')
        if b['daily_amount'] is not None:
            if any(a.get('currency')!=c['rate']['currency'] or a.get('reserved_amount') is None for a in attempts):raise ValueError('当日有未知金额或不同币种待核对，无法计算金额预算')
            if sum(a['reserved_amount'] for a in attempts)+money>b['daily_amount']:raise ValueError('超过每日金额预算（含预占）')
    def submit(self,id,confirmation):
        with self.lock:
            q=self.store.get('s2_quote',id)
            if not q:raise ValueError('请求预览不存在')
            if q['state']=='SUBMITTED':return q
            if not confirmation.confirmed:raise ValueError('需明确确认本次文案、声音及请求范围')
            if time.time()-q['created_epoch']>900 or q['config_revision']!=self.config(q['provider'])['revision']:raise ValueError('配置或预览已更新，请重新预览')
            new=[];mapping={}
            for item in q['items']:
                d=item['payload']
                if q['kind']=='tts' and d.get('tts_text',d['text'])!=prepare_tts_text(d['text']):raise ValueError('文本清洗规则已更新，请重新预览实际送入 TTS 的文本')
                if d.get('script_id'):
                    s=self.store.get('s2_script',d['script_id'])
                    if not s or s['revision']!=d['revision']:raise ValueError('文案已修改，请重新预览')
                state,a=self.cached(item['fingerprint'])
                if state=='BILLING_UNCERTAIN':raise ValueError('相同请求扣费不确定，请先在用量页核对，禁止盲目重试')
                if state=='NEW':new.append(item)
                else:mapping[item['fingerprint']]=a['id']
            if new and any(i['estimated_amount'] is None for i in new) and q['provider']!='fake' and not confirmation.accept_unknown_cost:raise ValueError('费用未知，请确认本次请求数与字符范围')
            self.check_budget(q,new)
            provider=self.provider(q['provider']);frozen_config=self.config(q['provider'])
            pending=[]
            for item in new:
                d=item['payload'];a=dict(id=uid(),fingerprint=item['fingerprint'],provider=q['provider'],scope=q['scope'],model_id=d['model_id'],kind=q['kind'],task_id=q['task_id'],batch_id=q['id'],status='QUEUED',cost_state='ESTIMATED' if item['estimated_amount'] is not None else 'UNKNOWN',estimated_amount=item['estimated_amount'],reserved_amount=item['estimated_amount'],currency=q['currency'],characters=item['characters'],started_at=stamp(),usage={},retry_count=0,request_id=None)
                self.store.put('s2_attempt',a);mapping[item['fingerprint']]=a['id'];pending.append((a,d))
            q.update(state='SUBMITTED',attempt_ids=list(dict.fromkeys(mapping.values())),submitted_at=stamp(),new_requests=len(new));self.store.put('s2_quote',q)
            for a,d in pending:self.pool.submit(self.execute,a['id'],d,provider,frozen_config)
            return q
    def execute(self,id,payload,provider,config):
        with self.lock:
            a=self.store.get('s2_attempt',id)
            if a['status']!='QUEUED':return
            a.update(status='RUNNING',dispatched_at=stamp());self.store.put('s2_attempt',a)
        started=time.monotonic()
        try:
            result=provider.tts(payload) if a['kind']=='tts' else provider.localize(payload)
            rid=uid();record=dict(id=rid,kind=a['kind'],provider=a['provider'],scope=a['scope'],model_id=a['model_id'],fingerprint=a['fingerprint'],payload=payload,created_at=stamp(),fixture=a['provider']=='fake',task_ids=[a['task_id']] if a['task_id'] else [])
            if a['kind']=='tts':
                record.update(self.publish_audio(rid,result))
                record['audio_review']=dict(status='REVIEW',message='待人工试听确认；技术成功不代表试听通过。')
            else:record['result']=result['result']
            with self.lock:
                self.store.put('s2_result',record)
                a=self.store.get('s2_attempt',id);cost=self.estimate(config,a['kind'],payload,result['usage'])
                a.update(status='SUCCEEDED',result_id=rid,usage=result['usage'],request_id=result.get('request_id'),cost_state='ESTIMATED' if cost is not None else 'UNKNOWN',reserved_amount=cost if cost is not None else a['reserved_amount'],settled_amount=cost,ended_at=stamp(),elapsed_seconds=round(time.monotonic()-started,3))
                self.store.put('s2_attempt',a)
        except Exception as error:
            uncertain=True;code='LOCAL_RESULT_ERROR';request_id=None
            if isinstance(error,ProviderError):uncertain=error.uncertain;code=error.code;request_id=error.request_id
            with self.lock:
                a=self.store.get('s2_attempt',id);a.update(status='BILLING_UNCERTAIN' if uncertain else 'FAILED',error=code,cost_state='BILLING_UNCERTAIN' if uncertain else 'UNKNOWN',ended_at=stamp(),request_id=request_id,elapsed_seconds=round(time.monotonic()-started,3))
                if not uncertain:a['reserved_amount']=0
                self.store.put('s2_attempt',a)
    def publish_audio(self,rid,result):
        temp=self.data/'audio'/f'{rid}.partial.mp3';out=temp.with_name(rid+'.mp3')
        try:
            temp.write_bytes(result['audio'])
            r=subprocess.run([FFPROBE,'-v','error','-show_format','-show_streams','-of','json',str(temp)],capture_output=True,timeout=20)
            info=json.loads(r.stdout);duration=float(info['format']['duration'])
            if r.returncode or not any(s['codec_type']=='audio' for s in info['streams']) or not 0<duration<3600:raise ValueError()
            subprocess.run([FFMPEG,'-v','error','-i',str(temp),'-f','null','-'],capture_output=True,check=True,timeout=60)
            alignment={k:result.get(k) for k in ['alignment','normalized_alignment']}
            timing='READY' if self.valid_alignment(alignment,duration) else 'NOT_READY'
            alignment_path=out.with_suffix('.alignment.json');alignment_path.write_text(json.dumps(dict(result_id=rid,duration=duration,timing=timing,**alignment),ensure_ascii=False))
            os.replace(temp,out)
            return dict(path=str(out),duration=duration,sha256=hashlib.sha256(out.read_bytes()).hexdigest(),timing=timing,alignment_path=str(alignment_path),text_normalized=''.join((result.get('normalized_alignment') or {}).get('characters',[])) or None)
        finally:temp.unlink(missing_ok=True)
    @staticmethod
    def valid_alignment(align,duration):
        a=align.get('normalized_alignment') or align.get('alignment')
        if not a:return False
        try:
            chars=a['characters'];start=a['character_start_times_seconds'];end=a['character_end_times_seconds']
            return bool(chars) and len(chars)==len(start)==len(end) and all(isinstance(ch,str) for ch in chars) and all(0<=s<=e<=duration+.15 for s,e in zip(start,end)) and all(s<=t for s,t in zip(start,start[1:]))
        except (KeyError,TypeError,ValueError):return False
    def cancel(self,id):
        with self.lock:
            a=self.store.get('s2_attempt',id)
            if not a:raise ValueError('请求不存在')
            if a['status']=='QUEUED':a.update(status='CANCELLED',reserved_amount=0,ended_at=stamp())
            elif a['status']=='RUNNING':a.update(status='BILLING_UNCERTAIN',cost_state='BILLING_UNCERTAIN',cancel_requested=True,error='请求已发出；取消不保证免收费，等待结果或核对账单')
            self.store.put('s2_attempt',a);return a
    def reconcile(self,id,amount):
        with self.lock:
            a=self.store.get('s2_attempt',id)
            if not a or a['cost_state']!='BILLING_UNCERTAIN':raise ValueError('没有待核对的请求')
            if a.get('cancel_requested') and not a.get('ended_at'):raise ValueError('请求仍在返回中，请稍后核对')
            if amount<0:raise ValueError('金额不能为负')
            a.update(reconciled=True,cost_state='PROVIDER_REPORTED',reserved_amount=amount,settled_amount=amount,reconciled_at=stamp(),reconciliation_source='用户根据供应商账单填写')
            return self.store.put('s2_attempt',a)
    def usage(self):
        attempts=self.store.all('s2_attempt');now=datetime.now(timezone.utc)
        def group(rows):
            return dict(requests=len(rows),active_paid=sum(bool(a['provider']!='fake' and (a['status'] in ACTIVE or (a.get('cancel_requested') and not a.get('ended_at')))) for a in rows),active=sum(bool(a['status'] in ACTIVE or (a.get('cancel_requested') and not a.get('ended_at'))) for a in rows),characters=sum(a['characters'] for a in rows if a['kind']=='tts' and a['status'] in HELD),input_tokens=sum(a['usage'].get('prompt_tokens') or 0 for a in rows),output_tokens=sum(a['usage'].get('completion_tokens') or 0 for a in rows),unknown_cost=sum(a['cost_state'] in ['UNKNOWN','BILLING_UNCERTAIN'] for a in rows),estimated_by_currency={cur:sum(a.get('reserved_amount') or 0 for a in rows if a['currency']==cur) for cur in {a['currency'] for a in rows if a.get('reserved_amount') is not None}})
        today=[a for a in attempts if self.today(self.config(a['provider']),a['started_at'])==self.today(self.config(a['provider']))]
        month=[a for a in attempts if self.today(self.config(a['provider']),a['started_at']).strftime('%Y-%m')==self.today(self.config(a['provider'])).strftime('%Y-%m')]
        recent=[a for a in attempts if (now-datetime.fromisoformat(a['started_at'])).total_seconds()<3600]
        return dict(today=group(today),month=group(month),hour=group(recent),providers={p:group([a for a in attempts if a['provider']==p]) for p in ['elevenlabs','deepseek','fake']},batches={q['id']:group([a for a in attempts if a['batch_id']==q['id']]) for q in self.store.all('s2_quote') if q['state']=='SUBMITTED'},attempts=attempts,account_balance='以设置页读取的供应商回执为准；本页仅记录本应用')
