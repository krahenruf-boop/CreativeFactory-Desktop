"""Local CTA assets and immutable task/plan snapshots; no remote resource loaders."""
import hashlib, io, json, math, os, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from .cta import CTAConfig, CTA_MOTION, PURCHASE_CTA

MAX_BYTES=20*1024*1024
FORMATS={'.gif':('GIF','image/gif'), '.png':('PNG','image/png'), '.webp':('WEBP','image/webp')}
CANVAS=(1080,1920)
LOCK=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat()
def digest(data): return hashlib.sha256(data).hexdigest()

def inspect_image(raw, suffix, mime=None):
    if suffix not in FORMATS or not raw or len(raw)>MAX_BYTES: raise ValueError('请选择20MB以内的GIF、PNG或WebP文件')
    fmt,expected=FORMATS[suffix]
    if mime and mime not in [expected,'application/octet-stream']: raise ValueError('图片MIME与扩展名不一致')
    try:
        with Image.open(io.BytesIO(raw), formats=[fmt]) as image:
            w,h=image.size; count=getattr(image,'n_frames',1)
            if image.format!=fmt or not 0<w<=4096 or not 0<h<=4096 or not 1<=count<=600 or w*h*count>40_000_000:
                raise ValueError('图片尺寸、帧数或解码总量超出上限（4096×4096 / 600帧）')
            delays=[];alpha=False;visible=False
            for index in range(count):
                image.seek(index);image.load();frame=image.convert('RGBA')
                low,high=frame.getchannel('A').getextrema();alpha|=low<255;visible|=high>0
                delays.append(max(10,int(image.info.get('duration',100) or 100)))
            if not visible: raise ValueError('图片完全透明，没有可显示的箭头')
            if count>1 and sum(delays)>60_000: raise ValueError('动态箭头单次循环请控制在60秒以内')
    except (OSError,EOFError,UnidentifiedImageError,Image.DecompressionBombError) as exc:
        raise ValueError('图片损坏或实际格式不匹配') from exc
    return dict(mime_type=expected,kind='animated' if count>1 else 'static',width=w,height=h,
                duration=sum(delays)/1000 if count>1 else None,frame_count=count,has_alpha=alpha)

class CTAAssets:
    def __init__(self,data,store=None):
        self.data=Path(data);self.store=store
        self.directory=self.data/'cta_assets';self.cache=self.data/'cache/cta'
    def assets(self):return self.store.all('cta_asset')
    def asset(self,id):
        row=self.store.get('cta_asset',id)
        if not row:raise ValueError('CTA素材不存在')
        return row
    def path(self,row):
        id=row['id'];ext=Path(row['file_path']).suffix
        if len(id)!=32 or any(c not in '0123456789abcdef' for c in id) or ext not in FORMATS:raise ValueError('CTA素材路径无效')
        path=self.directory/(id+ext)
        if str(path)!=row['file_path'] or not path.is_file() or path.is_symlink() or digest(path.read_bytes())!=row['sha256']:
            raise ValueError('CTA素材已缺失或改变，请重新上传')
        return path
    def upload(self,raw,filename,mime=None,name=None):
        ext=Path(filename).suffix.lower();meta=inspect_image(raw,ext,mime)
        name=(name or Path(filename).stem).strip()
        if not name or len(name)>80:raise ValueError('箭头名称需为1～80个字符')
        with LOCK:
            id=uuid.uuid4().hex;self.directory.mkdir(parents=True,exist_ok=True)
            path=self.directory/(id+ext);path.write_bytes(raw)
            row=dict(id=id,name=name,file_path=str(path),sha256=digest(raw),created_at=now(),**meta)
            try:return self.store.put('cta_asset',row)
            except Exception:path.unlink(missing_ok=True);raise
    def delete_asset(self,id):
        with LOCK:
            row=self.asset(id)
            def references(value):
                if isinstance(value,dict):return value.get('asset_id')==id or any(references(v) for v in value.values())
                return isinstance(value,list) and any(references(v) for v in value)
            for kind in ['cta_preset','preset','task','s3_plan']:
                if any(references(r) for r in self.store.all(kind)):
                    raise ValueError('该素材仍被预设、任务或历史计划引用，请先更换；历史资源不会删除')
            self.path(row).unlink();self.store.delete('cta_asset',id)
    def prepare(self,config,asset=None):
        c=CTAConfig.model_validate(config).model_copy(deep=True)
        if not c.asset_id:raise ValueError('请先选择箭头素材')
        row=asset or self.asset(c.asset_id)
        if row['id']!=c.asset_id:raise ValueError('CTA素材绑定不一致')
        path=self.path(row)
        if row['kind']=='animated':c.animation.type='source'
        elif c.animation.type=='source':c.animation.type='none'
        key=digest(json.dumps([row['sha256'],float(c.scale),float(c.rotation),float(c.opacity),c.animation.type,'cta-sprite-v1'],sort_keys=True).encode())
        directory=self.cache/key
        with LOCK:
            if not (directory/'metadata.json').is_file():self._sprite(path,row,c,directory)
        meta=json.loads((directory/'metadata.json').read_text())
        mx,my=meta['width']/CANVAS[0]*50,meta['height']/CANVAS[1]*50
        c.position_x=round(min(100-mx,max(mx,c.position_x)),5)
        c.position_y=round(min(100-my,max(my,c.position_y)),5)
        warning='当前箭头接近TikTok UI区域' if (c.position_x-mx<8 or c.position_x+mx>85 or c.position_y-my<8 or c.position_y+my>85) else ''
        return dict(key=key,config=c.model_dump(),asset=row,**meta,warning=warning,
                    url=f'/api/cta/sprites/{key}/sprite.png')
    def _sprite(self,path,row,c,directory):
        frames=[];delays=[]
        with Image.open(path) as source:
            if row['kind']=='animated':
                for index in range(source.n_frames):
                    source.seek(index);frames.append(source.convert('RGBA'));delays.append(max(10,int(source.info.get('duration',100) or 100)))
            else:frames=[source.convert('RGBA')];delays=[1000]
        motion=CTA_MOTION.get(c.animation.type)
        if motion:
            count=round(motion['period']*30);frames=frames*count
            delays=[round((i+1)*motion['period']*1000/count)-round(i*motion['period']*1000/count) for i in range(count)]
        transformed=[];sizes=[]
        for i,frame in enumerate(frames):
            phase=2*math.pi*i/len(frames);scale=1;angle=c.rotation;offset=0
            if c.animation.type=='pulse':scale=1+motion['amount']*(1-math.cos(phase))/2
            if c.animation.type=='wiggle':angle+=motion['amount']*math.sin(phase)
            if c.animation.type in ['bounce','float']:offset=round(CANVAS[1]*motion['amount']*math.sin(phase))
            w=round(CANVAS[0]*c.scale*scale);h=round(w*row['height']/row['width'])
            if h>CANVAS[1] or w>CANVAS[0]:raise ValueError('箭头超出画布，请减小大小或选择更合适比例的图片')
            if w*h*len(frames)>40_000_000:raise ValueError('动画解码总量过大，请减小箭头大小')
            image=frame.resize((w,h),Image.Resampling.LANCZOS).rotate(-angle,Image.Resampling.BICUBIC,expand=True)
            image.putalpha(image.getchannel('A').point(lambda a:round(a*c.opacity)))
            transformed.append((image,offset));sizes.append((image.width,image.height+2*abs(offset)))
        width=max(w for w,h in sizes);height=max(h for w,h in sizes)
        if width>CANVAS[0] or height>CANVAS[1]:raise ValueError('旋转后的箭头超出画布，请减小大小')
        sprites=[]
        for image,offset in transformed:
            sprite=Image.new('RGBA',(width,height));sprite.alpha_composite(image,((width-image.width)//2,(height-image.height)//2+offset));sprites.append(sprite)
        distinct=len({digest(f.tobytes()) for f in sprites})
        if row['kind']=='animated' and distinct<2:raise ValueError('GIF/动态图片没有有效帧变化，CTA技术QA未通过')
        directory.mkdir(parents=True,exist_ok=True)
        # APNG retains RGBA and each source frame's duration; browser and FFmpeg share this file.
        sprites[0].save(directory/'sprite.png',save_all=len(sprites)>1,append_images=sprites[1:],duration=delays,loop=0,disposal=0,blend=0)
        meta=dict(width=width,height=height,animated=len(sprites)>1,frame_count=len(sprites),cycle_seconds=sum(delays)/1000,
                  qa=dict(visible=any(f.getbbox() for f in sprites),inside_canvas=True,frame_variation=distinct>1 if len(sprites)>1 else True,
                          alpha_preserved=any(f.getchannel('A').getextrema()[0]<255 for f in sprites) if row['has_alpha'] else True))
        (directory/'metadata.json').write_text(json.dumps(meta))
    def bind_style(self,style):
        if style.cta and style.cta.asset_id:
            prepared=self.prepare(style.cta)
            style=style.model_copy(update={'cta':CTAConfig(**prepared['config'])})
        return style
    def plan(self,style,duration):
        if not style.cta or not style.cta.asset_id:
            return dict(enabled=style.arrow,start=max(0,duration-style.last_seconds),end=duration,duration=min(duration,style.last_seconds),template=PURCHASE_CTA.copy())
        prepared=self.prepare(style.cta);c=prepared['config'];length=min(duration,c['duration_seconds'])
        return dict(enabled=c['enabled'],start=max(0,duration-length),end=duration,duration=length,
                    snapshot=c,asset=prepared['asset'],warning=prepared['warning'])

def overlay_input(plan,data,index):
    """Only executes the CTA snapshot already chosen by the plan."""
    prepared=CTAAssets(data).prepare(plan['snapshot'],plan['asset']);c=prepared['config']
    path=Path(data)/'cache/cta'/prepared['key']/'sprite.png'
    args=['-ignore_loop','0','-i',str(path)] if prepared['animated'] else ['-loop','1','-i',str(path)]
    x=round(CANVAS[0]*c['position_x']/100-prepared['width']/2);y=round(CANVAS[1]*c['position_y']/100-prepared['height']/2)
    source=f'[{index}:v]setpts=PTS-STARTPTS+{plan["start"]}/TB[ctaasset]'
    overlay=f"overlay=x={x}:y={y}:eof_action=pass:enable='gte(t,{plan['start']})*lt(t,{plan['end']})*{int(plan['enabled'])}'"
    return args,source,overlay,prepared

def render_test(library,config):
    """One local six-second QA clip; never a task/batch submission."""
    import subprocess
    from . import media
    c=CTAConfig.model_validate(config).model_copy(update={'duration_seconds':2,'enabled':True})
    prepared=library.prepare(c);c=prepared['config']
    plan=dict(snapshot=c,asset=prepared['asset'],enabled=True,start=4,end=6)
    id=uuid.uuid4().hex;directory=library.data/'outputs/cta_tests'/id;directory.mkdir(parents=True)
    args,source,overlay,_=overlay_input(plan,library.data,1)
    output=directory/'cta_6s.mp4'
    command=[media.FFMPEG,'-hide_banner','-nostdin','-v','error','-f','lavfi','-i','color=c=0x68737c:s=1080x1920:r=30:d=6',*args,
             '-filter_complex_threads','1','-filter_complex',source+';[0:v][ctaasset]'+overlay+'[v]',
             '-map','[v]','-t','6','-r','30','-c:v','libx264','-preset','veryfast','-crf','18','-threads','2','-pix_fmt','yuv420p','-movflags','+faststart',str(output)]
    media.run(command,90)
    info=media.probe(output)
    media.run([media.FFMPEG,'-v','error','-i',str(output),'-f','null','-'],60)
    for name,t in [('before',3),('during',4.5),('later',5.5)]:
        media.run([media.FFMPEG,'-v','error','-ss',str(t),'-i',str(output),'-frames:v','1',str(directory/(name+'.png'))],30)
    from PIL import ImageChops
    with Image.open(directory/'before.png') as a,Image.open(directory/'during.png') as b,Image.open(directory/'later.png') as d:
        visible=bool(ImageChops.difference(a,b).getbbox())
        changed=bool(ImageChops.difference(b,d).getbbox())
    timing=test_window(output)
    checks=dict(file=output.stat().st_size>0,decode=True,resolution=(info['width'],info['height'])==(1080,1920),duration=abs(info['duration']-6)<.04,
                cta_window=timing['pass'],overlay_visible=visible,animated_change=changed if prepared['animated'] else True,**prepared['qa'])
    qa=dict(status='PASS' if all(checks.values()) else 'FAIL',checks=checks,test_only=True,config=c,
            source_sha256=prepared['asset']['sha256'],kind=prepared['asset']['kind'],duration=info['duration'],fps=info['fps'],timing=timing,human_review='PENDING')
    (directory/'qa.json').write_text(json.dumps(qa,ensure_ascii=False,indent=2))
    result=dict(id=id,path=str(output),screenshot=str(directory/'during.png'),qa=qa,created_at=now())
    return library.store.put('cta_test',result)

def test_window(output):
    """Measure the encoded test clip's first visible CTA frame against its plain background."""
    from . import media
    raw=media.run([media.FFMPEG,'-v','error','-i',str(output),'-vf','fps=30,scale=108:192','-f','rawvideo','-pix_fmt','rgb24','-'],30)
    size=108*192*3;frames=[raw[i:i+size] for i in range(0,len(raw),size)]
    baseline=frames[0]
    active=[i for i,frame in enumerate(frames) if any(abs(a-b)>20 for a,b in zip(frame,baseline))]
    return dict(first_visible=active[0]/30 if active else None,last_visible=active[-1]/30 if active else None,
                **{'pass':len(frames)==180 and bool(active) and 120<=active[0]<=121 and active[-1]==179})
