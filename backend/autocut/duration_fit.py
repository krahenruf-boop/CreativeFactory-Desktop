"""Local, pitch-preserving duration fit. Never rewrites confirmed copy or calls TTS.

Speeding up pictures SHORTENS coverage. Thus voice/video must be >= voice/visual;
applying the same speed to both cannot close a duration deficit.
"""
import math
VIDEO_SPEED_HARD_MAX=1.50
VOICE_SPEED_HARD_MAX=1.50
VIDEO_SPEED_NORMAL_MAX=1.35
VOICE_SPEED_NORMAL_MAX=1.25
VERSION='DURATION_FIT_V1'

def solve(visual,voice,task=None,music_only=False):
 if not all(isinstance(n,(int,float)) and math.isfinite(n) and n>0 for n in (visual,voice)):raise ValueError('DURATION_INPUT_INVALID')
 task=task or {};v=1.;a=1.;usable=math.floor(visual*30+1e-7)/30
 # Frame-ceiling retains the last phoneme, including the exact 1.50x boundary.
 required=voice if not music_only else usable
 ratio=required/usable
 fits=music_only or ratio<=VOICE_SPEED_HARD_MAX
 if not music_only and fits:
  if ratio>1:
   # Gentle joint pacing only where it leaves ample voice headroom. Detailed
   # demonstration/face shots and near-limit speech keep the picture at 1x.
   conservative=task.get('conservative_motion',False)
   ceiling=VOICE_SPEED_NORMAL_MAX if ratio<=VOICE_SPEED_NORMAL_MAX else VOICE_SPEED_HARD_MAX
   v=1. if conservative else 1.+min(.02,max(0,(ceiling/ratio-1)*.15))
   a=min(VOICE_SPEED_HARD_MAX,ratio*v)
  else:
   v=min(VIDEO_SPEED_NORMAL_MAX,max(1.,float(task.get('video_speed_preference',1.08))),usable/required)
  duration=math.ceil(voice/a*30-1e-9)/30
  if duration*v>usable:v=max(1.,usable/duration)
 else:duration=usable if music_only else voice/VOICE_SPEED_HARD_MAX;a=1. if music_only else VOICE_SPEED_HARD_MAX
 return dict(version=VERSION,original_visual_duration=visual,original_voice_duration=voice,video_speed=v,voice_speed=a,final_visual_duration=duration if fits else usable/v,final_voice_duration=duration if music_only else voice/a,target_duration=duration,fit_result='FIT' if fits else 'DURATION_FIT_FAILED',fits=fits,status='READY' if fits else 'DURATION_FIT_FAILED',tts_duration=round(voice,2),available_visual_duration=round(visual,2),missing_duration=round(max(0,voice/VOICE_SPEED_HARD_MAX-usable),2),audio_filter=f'atempo={a:.12f}',script_preserved=True)
