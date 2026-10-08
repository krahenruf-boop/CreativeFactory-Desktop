import hashlib, json, math, os, shutil, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from .models import Style
from .cta import arrow_points
from .mpt_text import wrap_text, subtitle_font_supports_text

FFMPEG = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
FFPROBE = shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'
FONT = '/System/Library/Fonts/STHeiti Medium.ttc'
W, H = 1080, 1920
EXTENSIONS = {'.mp4','.mov','.m4v','.mkv','.webm','.avi'}

def run(args, timeout=120):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode: raise ValueError('媒体处理失败：文件可能损坏或格式不支持。请重新导入可播放的视频。')
    return result.stdout

def probe(path):
    info=json.loads(run([FFPROBE,'-v','error','-protocol_whitelist','file,pipe','-show_streams','-show_format','-of','json',str(path)],30))
    video=next((s for s in info['streams'] if s['codec_type']=='video'),None)
    if not video: raise ValueError('文件没有视频画面')
    duration=float(info.get('format',{}).get('duration') or video.get('duration') or 0)
    if not math.isfinite(duration) or duration<=0: raise ValueError('视频时长不可用')
    rotation=next((s.get('rotation',0) for s in video.get('side_data_list',[]) if 'rotation' in s),0)
    return dict(duration=duration,width=video['width'],height=video['height'],fps=video.get('avg_frame_rate','未知'),rotation=rotation,audio=any(s['codec_type']=='audio' for s in info['streams']))

def fingerprint(path):
    s=Path(path).stat()
    return {'file_size':s.st_size,'mtime_ns':s.st_mtime_ns}

def source_state(asset):
    p=Path(asset['path'])
    if not p.is_file() or p.is_symlink(): return 'MISSING'
    try: return 'OK' if fingerprint(p)==asset['fingerprint'] else 'CHANGED'
    except OSError: return 'MISSING'

def thumbnail(path,out):
    run([FFMPEG,'-v','error','-nostdin','-protocol_whitelist','file,pipe','-i',str(path),'-frames:v','1','-vf','scale=360:640:force_original_aspect_ratio=decrease','-y',str(out)],30)

def layers(style:Style, cache:Path):
    # One raster renderer drives both browser preview and final FFmpeg overlays.
    key=hashlib.sha256((style.model_dump_json()+'renderer-1.0').encode()).hexdigest()
    directory=cache/key
    if (directory/'arrow.png').exists(): return key
    title=Image.new('RGBA',(W,H)); draw=ImageDraw.Draw(title)
    if style.title:
        if not subtitle_font_supports_text(FONT,style.title): raise ValueError('本机字体缺少标题字形，请更换文字（S1 暂不支持 emoji）')
        available=int(2*min(style.x,1-style.x)*W-60)
        text='\n'.join(wrap_text(line,available,FONT,style.size)[0] for line in style.title.split('\n'))
        font=ImageFont.truetype(FONT,style.size)
        box=draw.multiline_textbbox((0,0),text,font=font,spacing=12,stroke_width=style.stroke,align='center')
        width,height=box[2]-box[0],box[3]-box[1]
        x=style.x*W-width/2-box[0]; y=style.y*H-box[1]
        if width>available+2 or y+box[3]>H-40: raise ValueError('标题超出画布，请减小字号或调整位置')
        draw.multiline_text((x,y),text,font=font,fill=style.color,stroke_width=style.stroke,stroke_fill=style.stroke_color,spacing=12,align='center')
    arrow=Image.new('RGBA',(W,H)); d=ImageDraw.Draw(arrow)
    # Reuse the original polygon, rotated in the shared reference canvas.
    points=arrow_points(style)
    d.polygon(points,fill=style.arrow_color,outline='#20242c',width=4)
    directory.mkdir(parents=True,exist_ok=True)
    title.save(directory/'title.png'); arrow.save(directory/'arrow.png')
    return key

def render(asset, style, duration, output, cache):
    if source_state(asset)!='OK': raise ValueError('原素材已移动或修改，请重新定位后再导出')
    if duration>asset['metadata']['duration']+0.015: raise ValueError('素材短于导出时长，请减小时长或更换素材')
    if style.title and (style.start>=duration or style.end>duration): raise ValueError('标题时窗超出短片，请调整结束时间')
    if not (style.cta and style.cta.asset_id) and style.arrow and style.last_seconds>duration: raise ValueError('箭头显示时长不能超过短片时长')
    key=layers(style,cache); d=cache/key
    filters=f'[0:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x17181b,setsar=1,fps=30[base];[base][1:v]overlay=0:0:enable=\'gte(t,{style.start})*lt(t,{style.end})\'[t];[t][2:v]overlay=0:0:enable=\'gte(t,{max(0,duration-style.last_seconds)})*lt(t,{duration})*{int(style.arrow)}\'[out]'
    cta_args=['-loop','1','-i',str(d/'arrow.png')]
    if style.cta and style.cta.asset_id:
        from .cta_assets import CTAAssets, overlay_input
        from .s2.engine import Store
        data=cache.parents[1];cta=CTAAssets(data,Store(data/'creative_factory.sqlite3')).plan(style,duration)
        cta_args,source,overlay,_=overlay_input(cta,data,2)
        filters=filters[:filters.index(';[t][2:v]')]+';'+source+';[t][ctaasset]'+overlay+'[out]'
    temp=output.with_suffix('.partial.mp4')
    try:
        run([FFMPEG,'-v','error','-nostdin','-threads','2','-protocol_whitelist','file,pipe','-i',asset['path'],'-loop','1','-i',str(d/'title.png'),*cta_args,'-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-filter_complex_threads','1','-filter_complex',filters,'-map','[out]','-map','3:a','-t',str(duration),'-c:v','libx264','-preset','fast','-crf','20','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart','-y',str(temp)],180)
        info=probe(temp)
        if abs(info['duration']-duration)>0.1: raise ValueError('成片时长校验失败')
        run([FFMPEG,'-v','error','-i',str(temp),'-f','null','-'],60)
        os.replace(temp,output)
        return info,key
    finally:
        temp.unlink(missing_ok=True)
