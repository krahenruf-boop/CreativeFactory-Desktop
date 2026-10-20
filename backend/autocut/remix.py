"""Bounded cut-list adapter for the existing shared montage renderer."""
import copy,math
from pydantic import BaseModel,ConfigDict,Field

class Cut(BaseModel):
 model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
 source_rank:int=Field(ge=1)
 start:float=Field(ge=0)
 end:float=Field(gt=0)
 reason:str=Field(min_length=1)

class RemixOutput(BaseModel):
 model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
 rank:int=Field(ge=1,le=30)
 primary_source_rank:int=Field(ge=1)
 angle:str=Field(min_length=1)
 clips:list[Cut]=Field(min_length=2,max_length=12)

class RemixDecision(BaseModel):
 model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
 outputs:list[RemixOutput]=Field(min_length=1,max_length=30)

def remix_items(selection,sources,analyses,count,selling_points_primary=False):
 """Resolve rank-only model output into trusted paths; do not accept arbitrary files."""
 by_rank={s['rank']:s for s in sources if s['rank'] in analyses}
 if len(by_rank)<2:raise ValueError('REMIX_NEEDS_SOURCES')
 # Checkpoints carry trusted audit metadata beside the bounded model payload.
 # Keep rejecting unknown payload fields, including any model-provided paths.
 payload={k:v for k,v in selection.items() if k not in {'driver_provenance','source_arrow_hint','subject_protection'}}
 result=RemixDecision.model_validate(payload)
 if sorted(o.rank for o in result.outputs)!=list(range(1,count+1)):raise ValueError('REMIX_PLAN_INVALID')
 items=[];mapped={};seen=set()
 for o in sorted(result.outputs,key=lambda o:o.rank):
  if o.primary_source_rank not in by_rank:raise ValueError('REMIX_PLAN_INVALID')
  primary_analysis=analyses[o.primary_source_rank]
  if not primary_analysis['clean_script'].strip() and not (selling_points_primary and primary_analysis.get('selling_points') and primary_analysis.get('context')):raise ValueError('REMIX_PLAN_INVALID')
  used={c.source_rank for c in o.clips}
  if len(used)<2 or not used.issubset(by_rank) or o.primary_source_rank not in used:raise ValueError('REMIX_PLAN_INVALID')
  clips=[];ranges={}
  for c in o.clips:
   s=by_rank[c.source_rank]
   if c.end>s['duration'] or c.end-c.start<.5:raise ValueError('REMIX_PLAN_INVALID')
   if any(c.start<b and a<c.end for a,b in ranges.get(c.source_rank,[])):raise ValueError('REMIX_PLAN_INVALID')
   ranges.setdefault(c.source_rank,[]).append((c.start,c.end))
   clips.append(dict(source_rank=c.source_rank,path=s['path'],sha256=s['sha256'],source_duration=s['duration'],source_start=c.start,source_end=c.end,reason=c.reason,**({'source_health':s['source_health']} if s.get('source_health') else {})))
  from .source_library import trim_clips
  clips=trim_clips(clips)
  signature=tuple((c['sha256'],c['source_start'],c['source_end']) for c in clips)
  if signature in seen:raise ValueError('REMIX_PLAN_INVALID')
  seen.add(signature)
  primary=by_rank[o.primary_source_rank];a=copy.deepcopy(analyses[o.primary_source_rank])
  a['context']='Remix angle: '+o.angle+'\n'+ '\n'.join(f"Source {r}: {analyses[r]['context']}" for r in sorted(used))
  for key in ['selling_points','allowed_claims']:a[key]=list(dict.fromkeys(v for r in sorted(used) for v in analyses[r][key]))
  a['overlays_need_review']=any(analyses[r]['overlays_need_review'] for r in used)
  a['existing_arrow']=any(analyses[r]['existing_arrow'] for r in used)
  mapped[o.rank]=a
  items.append(dict(primary,rank=o.rank,filename=f'混剪_{o.rank:02}.mp4',duration=sum(c['source_end']-c['source_start'] for c in clips),primary_source=primary,remix_clips=clips,creative_angle=o.angle))
 return items,mapped

def apply_montage(plan,item):
 """Fit approved clips to the same voice timeline; renderer remains unchanged."""
 clips=item['remix_clips'];frames=round(plan['target_duration']*30)
 if frames<len(clips)*9:raise ValueError('REMIX_DURATION_REVIEW')
 total=sum(c['source_end']-c['source_start'] for c in clips)
 base_speed=min(1.25,plan['video_speed'],total/plan['target_duration'])
 if base_speed<.9:raise ValueError('REMIX_DURATION_REVIEW')
 desired=[(c['source_end']-c['source_start'])/total*frames for c in clips]
 allocation=[math.floor(n) for n in desired]
 for i in sorted(range(len(clips)),key=lambda i:desired[i]-allocation[i],reverse=True)[:frames-sum(allocation)]:allocation[i]+=1
 segments=[];cursor=0
 for c,n in zip(clips,allocation):
  if n<9:raise ValueError('REMIX_DURATION_REVIEW')
  end=min(c['source_end'],c['source_start']+(n/30)*base_speed)
  speed=(end-c['source_start'])/(n/30)
  if not .9<=speed<=1.25:raise ValueError('REMIX_DURATION_REVIEW')
  segments.append(dict(c,source_end=end,speed=speed,output_start=cursor/30,output_end=(cursor+n)/30));cursor+=n
 plan.pop('source_ranges',None)
 plan.update(mode='Remix',source=dict(item['primary_source'],source_end=item['primary_source']['duration']),montage_segments=segments,video_speed=1.,creative_angle=item['creative_angle'])
 from app.s3.headless_media import validate_montage
 validate_montage(plan)
 return plan
