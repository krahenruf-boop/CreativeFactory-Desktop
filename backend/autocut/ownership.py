"""Approval ownership and finite action transitions. Presentation reads never mutate jobs."""
import hashlib,json,math,subprocess,time
from functools import lru_cache
from pathlib import Path
from .script_language import target_language,script,check,issues as language_issues

VERSION='OWNERSHIP_V1'
ERRORS={'STATE_TRANSITION_REJECTED':'当前阶段尚未完成，不能将素材标记为完成','STALE_ACTION':'任务状态已变化，请刷新后重新操作','FINAL_NOT_READY':'成片或自动检查尚未完成，不能确认成片','FINAL_OUTPUT_INVALID':'成片文件暂不可用，请重新导出','OVERLAY_NOT_READY':'画面预览尚未准备完成','APPROVAL_REQUIRED':'请完成当前阶段的确认'}
ACTIVE={'VALIDATE_SUPPLEMENT':'VALIDATE_SUPPLEMENT','BUILD_SUPPLEMENT_PLAN':'BUILD_SUPPLEMENT_PLAN','WAITING':'ANALYZING','QUEUED':'QUEUED','ANALYZING':'ANALYZING','ASR':'ASR','SCRIPTING':'SCRIPTING','LOCALIZING':'LOCALIZING','COPY_READY':'SCRIPTING','DURATION_FIT':'DURATION_FIT','VOICE':'TTS','TTS_PROCESSING':'TTS','EDITING':'EDITING','OVERLAY':'OVERLAY','RENDERING':'RENDERING','CHECKING':'QA'}
LABELS={'VALIDATE_SUPPLEMENT':'正在验证视频','BUILD_SUPPLEMENT_PLAN':'正在匹配补充画面','DURATION_FIT':'正在自动适配时长','QUEUED':'等待系统处理','ANALYZING':'正在分析素材','ASR':'正在识别口播','SCRIPTING':'正在生成脚本','LOCALIZING':'正在生成目标市场语言脚本','TTS':'正在生成配音','EDITING':'正在自动剪辑','OVERLAY':'正在处理画面','RENDERING':'正在导出成片','QA':'正在进行质量检查','COMPLETED':'已完成','FAILED':'处理失败','PAUSED':'已暂停'}
NODES={
 'APPROVE_SCRIPT':('SCRIPTING','文案','等待确认脚本','最终口播脚本','请确认这条视频将使用的最终口播，确认后才会开始生成配音。','配音 → 剪辑 → 检查',['CHANGE_SCRIPT','APPROVE_SCRIPT']),
 'CHANGE_SCRIPT':('SCRIPTING','文案','等待更换脚本','最终口播脚本','口播尚未就绪，请更换脚本并核对目标语言与中文对照。','确认脚本后：配音 → 剪辑 → 检查',['CHANGE_SCRIPT']),
 'REPAIR_LOCALIZATION':('LOCALIZING','文案','等待修正口播语言','目标市场口播','最终口播语言不正确，请先转换为目标市场语言。','转换口播 → 确认脚本 → 配音 → 剪辑 → 检查',['REPAIR_LOCALIZATION','CHANGE_SCRIPT']),
 'APPROVE_TITLE':('SCRIPTING','文案','等待确认标题','视频标题','请选择最终视频前 5 秒使用的标题。','配音或剪辑 → 检查',['APPROVE_TITLE','REGENERATE_TITLE']),
 'ADD_SUPPLEMENT_MEDIA':('EDITING','剪辑','等待补充画面','补充视频','现有可用画面不足以覆盖口播，请补充同产品视频或缩短脚本。','检查画面时长 → 剪辑 → 检查',['ADD_SUPPLEMENT_MEDIA','SHORTEN_SCRIPT']),
 'REVIEW_OVERLAY':('OVERLAY','剪辑','等待检查画面','画面处理预览','原文字区域处理效果需要检查；接受当前画面只通过画面阶段。','导出 → 检查',['MODIFY_OVERLAY','APPROVE_OVERLAY']),
 'REVIEW_FINAL_VIDEO':('QA','检查','等待检查成片','最终成片','Creative Factory 已完成剪辑和自动检查，请查看最终视频。','确认成片后，该视频将标记为完成。',['MODIFY_FINAL','APPROVE_FINAL']),
 'RETRY_PROCESSING':('FAILED','检查','等待重新处理','未完成的处理阶段','该阶段未完成，请查看原因并重新处理当前素材。','完成当前阶段 → 后续剪辑与检查',['RETRY_PROCESSING']),
 'RESUME_PROCESSING':('PAUSED','分析','等待恢复处理','已暂停的素材','处理已暂停，恢复后由 Creative Factory 继续完成。','从已有进度恢复处理',['RESUME_PROCESSING']),
}

def digest(v):return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
def read(path):
 try:return json.loads(Path(path).read_text()) if path else {}
 except (OSError,ValueError,TypeError):return {}
def checkpoints(store,id,rank):
 with store.connect() as db:return {r['stage']:dict(input_sha=r['input_sha'],data=json.loads(r['data'])) for r in db.execute('SELECT stage,input_sha,data FROM checkpoints WHERE job_id=? AND item IN (?,?) ORDER BY length(item)',(id,str(int(rank)),f'{int(rank):02}'))}
def output_stat(path):
 try:
  p=Path(path);s=p.stat()
  return [str(p.resolve()),s.st_size,s.st_mtime_ns,s.st_ino] if p.is_file() else None
 except (OSError,TypeError):return None
@lru_cache(maxsize=256)
def probe_output(stat):
 from .context import RES
 try:
  p=Path(stat[0]);result=subprocess.run([str(RES/'runtime/media/bin/ffprobe'),'-v','error','-show_streams','-show_format','-of','json',str(p)],capture_output=True,text=True,timeout=15,check=True)
  info=json.loads(result.stdout);d=float(info.get('format',{}).get('duration',0));streams=info.get('streams',[])
  return math.isfinite(d) and d>0 and any(s.get('codec_type')=='video' and s.get('width',0)>0 and s.get('height',0)>0 for s in streams) and any(s.get('codec_type')=='audio' for s in streams)
 except (OSError,ValueError,subprocess.SubprocessError):return False

@lru_cache(maxsize=256)
def file_hash(stat):
 with open(stat[0],'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def final_ready(store,j,x,cp=None,verify=True):
 """All final transitions (including automatic PASS) use this production gate."""
 cp=cp if cp is not None else checkpoints(store,j['id'],x['rank'])
 qa=cp.get('QA_DONE',{}).get('data',{})
 if qa.get('status') not in {'PASS','WARNING'}:
  from .production_policy import classify
  legacy_completed=x.get('status') in {'PASS','USER_APPROVED'} and 'FINAL_APPROVED' in cp and qa.get('technical',{}).get('status')=='PASS' and classify(qa.get('visual') or qa)['status']=='WARNING'
  if not legacy_completed:return False
 if not all(k in cp for k in ('FINAL_RENDERED','QA_DONE')):return False
 stat=output_stat(x.get('output'))
 if not stat or language_issues(j,x):return False
 if x.get('script_status')=='WAITING_APPROVAL' or x.get('script_draft') and x['script_draft'].get('id')!=x.get('approved_draft_id'):return False
 plan=read(x.get('plan'))
 if not plan or str(plan.get('output'))!=str(x.get('output')):return False
 if plan.get('cta_policy',{}).get('version')=='CTA_REQUIRED_V1':
  from .cta_policy import required
  if required(plan):
   arrow=qa.get('cta') or {}
   if arrow.get('status')!='PASS' or arrow.get('output_sha256')!=file_hash(tuple(stat)):return False
 # Checkpoints must describe the same render revision, never an earlier export.
 if cp['FINAL_RENDERED']['input_sha']!=cp['QA_DONE']['input_sha']:return False
 rendered=cp['FINAL_RENDERED']['data']
 if rendered.get('output') and rendered['output']!=x['output']:return False
 if rendered.get('error'):return False
 expected=rendered.get('output_sha256') or (cp['QA_DONE']['data'].get('technical') or {}).get('output_sha256')
 if expected and file_hash(tuple(stat))!=expected:return False
 music=x.get('voiceover_mode')=='MUSIC_ONLY' or plan.get('voiceover_mode')=='MUSIC_ONLY'
 try:
  if not music:
   spoken=check(script(x),target_language(j))
   if 'TTS_DONE' not in cp or not plan.get('visual',{}).get('subtitle',{}).get('captions'):return False
   audio=x.get('audio_result') or cp['TTS_DONE']['data']
   if audio.get('technical_status')!='PASS' or not Path(audio.get('path','')).is_file():return False
   # Old plans can prove completion through matching audio + validated captions.
   if plan.get('final_script')!=spoken['text'] or audio.get('script_text',spoken['text'])!=spoken['text']:return False
   if plan.get('audio',{}).get('sha256')!=audio.get('sha256'):return False
  elif plan.get('visual',{}).get('subtitle',{}).get('captions'):return False
 except (ValueError,TypeError):return False
 return not verify or probe_output(tuple(stat))

def assert_final(store,j,x,previous=None):
 if previous is not None and previous not in {'CHECKING','REVIEW_REQUIRED','WAITING_FINAL_REVIEW','PASS','USER_APPROVED'}:raise ValueError('STATE_TRANSITION_REJECTED')
 if not final_ready(store,j,x):raise ValueError('FINAL_NOT_READY')

def stage_rows(cp,phase,action,done=False):
 active={'DURATION_FIT':3,'QUEUED':0,'ANALYZING':0,'ASR':0,'SCRIPTING':1,'LOCALIZING':1,'TTS':2,'EDITING':3,'OVERLAY':3,'RENDERING':3,'QA':4,'COMPLETED':4,'FAILED':4,'PAUSED':0}.get(phase,0)
 completed=[bool(cp.get('VIDEO_ANALYSIS')),bool(cp.get('COPY_DONE') or cp.get('SCRIPT_APPROVED')),bool(cp.get('TTS_DONE')),bool(cp.get('FINAL_RENDERED')),bool(cp.get('QA_DONE'))]
 return [dict(name=n,status='WAITING_USER' if action!='NONE' and i==active else 'DONE' if done or completed[i] and i!=active else 'PROCESSING' if i==active and phase not in {'FAILED','PAUSED'} else 'PENDING',label='等待你操作' if action!='NONE' and i==active else '已完成' if done or completed[i] and i!=active else '处理中' if i==active and phase not in {'FAILED','PAUSED'} else '未开始') for i,n in enumerate(['分析','文案','配音','剪辑','检查'])]

def item_state(store,j,x,cp=None):
 cp=cp if cp is not None else checkpoints(store,j['id'],x['rank']);s=x.get('status','WAITING');action='NONE';codes=' '.join(x.get('issues',[])+language_issues(j,x)).upper();draft=x.get('script_draft');candidate=bool(x.get('content_edit')) or x.get('voiceover_mode')=='MUSIC_ONLY' or bool(x.get('script_candidates')) or bool(draft and draft.get('id')!=x.get('approved_draft_id'))
 active=s in ACTIVE and (bool(x.get('operation_inflight')) or s=='QUEUED' and j['state']!='INTERRUPTED' or j['state'] in {'RUNNING','QUEUED','STOPPING'})
 # A stable per-item approval node takes precedence over other items running.
 waiting_script=s=='WAITING_SCRIPT_APPROVAL' or (s in {'COPY_READY','WAITING'} and candidate and x.get('script_status')!='USER_APPROVED' and j['state']=='WAITING_SCRIPT_APPROVAL')
 waiting_title=s=='WAITING_TITLE_APPROVAL' or (s in {'COPY_READY','WAITING'} and j['state']=='WAITING_TITLE_APPROVAL')
 ready=False
 if waiting_script:action='APPROVE_SCRIPT' if candidate else 'CHANGE_SCRIPT'
 elif waiting_title:action='APPROVE_TITLE'
 elif active:pass
 elif s in ACTIVE:action='RESUME_PROCESSING' if j['state']=='INTERRUPTED' else 'RETRY_PROCESSING'
 elif s=='WAITING_SUPPLEMENT_MEDIA' or any(c in codes for c in ['VISUAL_DURATION_INSUFFICIENT','DURATION_NEEDS_SUPPLEMENT','SUPPLEMENT_']):action='ADD_SUPPLEMENT_MEDIA'
 elif any(c in codes for c in ['SCRIPT_LANGUAGE','LOCALIZATION_REQUIRED']):action='REPAIR_LOCALIZATION'
 elif 'TITLE_LANGUAGE_MISMATCH' in codes:action='APPROVE_TITLE'
 elif draft and draft.get('id')!=x.get('approved_draft_id'):action='APPROVE_SCRIPT'
 elif s in {'REVIEW_REQUIRED','WAITING_FINAL_REVIEW','PASS','USER_APPROVED'}:
  ready=final_ready(store,j,x,cp)
  if ready:
   if s not in {'PASS','USER_APPROVED'}:action='REVIEW_FINAL_VIDEO'
  elif any(c in codes for c in ['SCRIPT','COPY','SPEECH','ASR','VOICE','TTS']):action='CHANGE_SCRIPT'
  elif 'TITLE' in codes:action='APPROVE_TITLE'
  elif any(c in codes for c in ['OVERLAY','GHOST','BLUR','PROPAINTER','WATERMARK','TEXT','SUBJECT']):action='REVIEW_OVERLAY'
  else:action='RETRY_PROCESSING'
 elif s in {'FAILED','INTERRUPTED'}:action='RETRY_PROCESSING' if s=='FAILED' else 'RESUME_PROCESSING'
 phase=ACTIVE.get(s,'COMPLETED' if ready and action=='NONE' else 'FAILED');owner='SYSTEM';allowed=[];heading='处理中';message=LABELS.get(phase,'等待系统处理');next_step='无需操作';obj='';stage={'TTS':'配音','EDITING':'剪辑','OVERLAY':'剪辑','RENDERING':'剪辑','QA':'检查','SCRIPTING':'文案','LOCALIZING':'文案','COMPLETED':'完成'}.get(phase,'分析')
 if s=='QUEUED':
  # A resumed item keeps completed stages; queueing never means ASR starts over.
  phase={'LOCALIZATION':'LOCALIZING','VISUAL':'OVERLAY','OVERLAY':'OVERLAY','SUPPLEMENT':'EDITING'}.get(x.get('resume_phase')) or ('SCRIPTING' if j.get('title_approval_mode')=='manual' and x.get('title_status')!='USER_APPROVED' and not j.get('titles_approved') else 'TTS' if 'SCRIPT_APPROVED' in cp else 'SCRIPTING' if 'VIDEO_ANALYSIS' in cp else 'ANALYZING')
  stage={'LOCALIZING':'文案','SCRIPTING':'文案','TTS':'配音','OVERLAY':'剪辑','EDITING':'剪辑','ANALYZING':'分析'}[phase];message='等待系统继续：'+stage
 if x.get('resume_phase')=='SUPPLEMENT' and s in {'QUEUED','EDITING'}:message='正在继续剪辑'
 if s in {'VALIDATE_SUPPLEMENT','BUILD_SUPPLEMENT_PLAN'}:stage='剪辑'
 if ready and s in {'PASS','USER_APPROVED'}:phase='COMPLETED';heading='已完成';message='成片和自动检查均已完成';next_step='无需操作';stage='完成'
 if phase in {'LOCALIZING','TTS'} and s!='QUEUED':
  lang=target_language(j);name={'pt-BR':'巴西葡萄牙语','es-MX':'墨西哥西班牙语'}.get(lang,lang);message='正在生成'+name+('脚本' if phase=='LOCALIZING' else '配音')
 if action!='NONE':
  phase,stage,heading,obj,message,next_step,allowed=NODES[action];allowed=list(allowed);owner='USER'
  if action=='APPROVE_SCRIPT' and not candidate:allowed=['CHANGE_SCRIPT']
  if action=='APPROVE_TITLE' and not x.get('title_candidates'):allowed=['REGENERATE_TITLE']
  if action=='REVIEW_OVERLAY' and not any(output_stat(x.get(k)) for k in ['overlay_preview','thumbnail','source_sheet']):allowed=['MODIFY_OVERLAY']
 if action in {'APPROVE_SCRIPT','APPROVE_TITLE'}:
  allowed+=['APPROVE_CONTENT','EDIT_CONTENT_TITLES']
  if 'CHANGE_SCRIPT' not in allowed:allowed.append('CHANGE_SCRIPT')
 from .batch_confirmation import batch_scope
 batch_phase,batch_ranks=batch_scope(j)
 if (j.get('batch_review_required') or j.get('batch_review')) and batch_phase in {'SCRIPTS','TITLES'} and x['rank'] in batch_ranks:
  allowed=[a for a in allowed if a not in {'APPROVE_CONTENT','APPROVE_SCRIPT','APPROVE_TITLE','REGENERATE_TITLE','EDIT_CONTENT_TITLES'}]
  if batch_phase=='TITLES' and not x.get('operation_inflight'):allowed=['EDIT_CONTENT_TITLES']
  if batch_phase=='SCRIPTS':
   if x.get('batch_script_approved'):allowed=[]
   elif not active and not x.get('operation_inflight'):
    # At the batch script barrier, wording suggestions are advisory. The user
    # can use the prepared manuscript or replace it; never approve the video.
    phase='SCRIPTING';stage='文案';owner='USER';action='CHANGE_SCRIPT';allowed=['CHANGE_SCRIPT']
    heading='口播确认';obj='最终口播脚本';message='查看口播与中文对照，不满意时可更换脚本。';next_step='确认全部口播 → 标题确认 → 剪辑生成'
 from .production_policy import approved_content
 if batch_phase=='RUNNING' and approved_content(x) and phase!='COMPLETED':
  allowed=[];owner='SYSTEM';action='NONE';heading='剪辑中';next_step='无需操作'
  if s=='DURATION_FIT':phase='DURATION_FIT';stage='剪辑';message='正在自动适配时长'
  elif not active:
   phase='FAILED';stage='检查';heading='失败';message={'CTA_OUT_OF_CANVAS':'画面布局自动修复失败','CTA_SUBTITLE_COLLISION':'画面布局自动修复失败','OUTPUT_QA_FAILED':'导出文件未通过完整性检查','SEVERE_VISUAL_QA':'成片存在严重画面问题，自动修复失败','MATCHED_VOICE_UNAVAILABLE':'当前账号缺少匹配的配音声音','AI_DRIVER_UNAVAILABLE':'AI 分析服务不可用'}.get(next(iter(x.get('issues',[])),''),'处理失败，请查看技术详情')
   if s=='WAITING_SUPPLEMENT_MEDIA' or any(c in codes for c in ['DURATION_FIT_FAILED','VISUAL_DURATION_INSUFFICIENT']):
    owner='USER';action='ADD_SUPPLEMENT_MEDIA';allowed=['ADD_SUPPLEMENT_MEDIA','CHANGE_SCRIPT'];phase='DURATION_FIT';stage='剪辑';heading='等待补充画面'
    gap=(x.get('duration_match') or {}).get('missing_duration',0)
    message=('补充画面仍不足，还缺少约 %.2f 秒有效画面' % gap) if gap else '请添加同产品的有效补充画面'
    if 'SUPPLEMENT_MEDIA_INVALID' in codes:message='补充文件无法读取，请重新选择视频'
    elif 'SUPPLEMENT_DOWNLOAD_FAILED' in codes:message='视频下载失败，请更换地址或选择本地视频'
    elif 'SUPPLEMENT_PLANNING_FAILED' in codes:message='补充画面匹配失败，请查看具体原因或更换视频'
   elif any(c in codes for c in ['SCRIPT_LANGUAGE','LOCALIZATION_REQUIRED']):
    owner='USER';action='REPAIR_LOCALIZATION';allowed=['REPAIR_LOCALIZATION','CHANGE_SCRIPT'];message='目标语言处理失败'
   elif 'TITLE_LANGUAGE_MISMATCH' in codes:
    owner='USER';action='REPAIR_TITLE';allowed=['EDIT_CONTENT_TITLES'];message='目标语言标题处理失败'
 if s=='FAILED' and action=='RETRY_PROCESSING':allowed=[];action='NONE';owner='SYSTEM';heading='失败';phase='FAILED'
 if phase=='COMPLETED':allowed=['EDIT_CONTENT']
 if j.get('deleted_at'):owner='SYSTEM';allowed=[];heading='已删除';next_step='恢复任务后可操作'
 token=digest([x.get('action_revision',0),s,action,allowed,x.get('script_draft',{}).get('id'),x.get('script_revision'),x.get('selected_title_id'),x.get('script_status'),x.get('output'),[(k,v['input_sha']) for k,v in sorted(cp.items())]])
 return dict(current_owner=owner,pipeline_status=phase,user_action_status=action,allowed_actions=allowed,action_token=token,state_title=heading,state_message=message,confirmation_object=obj,next_step=next_step,current_stage=stage,final_rendered='FINAL_RENDERED' in cp,qa_done='QA_DONE' in cp,stages=stage_rows(cp,phase,action,ready and s in {'PASS','USER_APPROVED'}),ownership_version=VERSION)

def require_action(store,id,rank,action,token=None):
 j=store.get(id);x=next(x for x in j['items'] if x['rank']==f'{int(rank):02}');state=item_state(store,j,x)
 if j.get('deleted_at'):raise ValueError('JOB_DELETED')
 if token is not None and token!=state['action_token']:raise ValueError('STALE_ACTION')
 if action not in state['allowed_actions']:raise ValueError('STALE_ACTION')
 return j,x,state

def present_ownership(store,raw,presented):
 states={x['rank']:item_state(store,raw,x) for x in raw['items']}
 from .content_confirmation import content_view
 from .copy_presentation import TranslationCache
 cache=TranslationCache(store.home)
 for x in presented['items']:
  x.update(states[x['rank']])
  x['ui_status']='已删除' if raw.get('deleted_at') else '已完成' if x['pipeline_status']=='COMPLETED' else '失败' if x['pipeline_status']=='FAILED' else '待确认' if 'APPROVE_CONTENT' in x['allowed_actions'] else '需要处理' if x['current_owner']=='USER' else '处理中'
  x['ui_stage']='内容' if x['current_stage']=='文案' else x['current_stage']
  x['ui_progress']=100 if x['ui_status']=='已完成' else {'分析':10,'内容':35,'配音':50,'剪辑':70,'检查':90}.get(x['ui_stage'],0)
  if x['pipeline_status']=='DURATION_FIT':x['ui_progress']=52
  elif x['pipeline_status']=='RENDERING':x['ui_progress']=88
  elif x['pipeline_status']=='QA':x['ui_progress']=92
  x['content_confirmation']=content_view(raw,x,cache)
  for step in x['stages']:
   if step['name']=='文案':step['name']='内容'
   if step['status']=='WAITING_USER':step['label']='待确认' if x['ui_status']=='待确认' else '需要处理'
 rows=presented['items'];counts=dict(processing=sum(x['current_owner']=='SYSTEM' and x['pipeline_status'] not in {'COMPLETED','FAILED','PAUSED'} for x in rows),waiting=sum(x['current_owner']=='USER' for x in rows),completed=sum(x['pipeline_status']=='COMPLETED' for x in rows))
 presented.update(ownership_counts=counts,current_owner='USER' if counts['waiting'] and not raw.get('deleted_at') else 'SYSTEM',state_title='待你处理' if counts['waiting'] else '已完成' if rows and counts['completed']==len(rows) else '已暂停' if raw['state']=='INTERRUPTED' else '处理中',has_user_actions=bool(counts['waiting']) and not raw.get('deleted_at'))
 presented['content_counts']={k:sum(x['ui_status']==label for x in rows) for k,label in [('processing','处理中'),('pending','待确认'),('completed','已完成'),('exception','需要处理')]}
 presented['ui_status']='已删除' if raw.get('deleted_at') else '处理中' if counts['processing'] else '待确认' if presented['content_counts']['pending'] else '需要处理' if counts['waiting'] else '已完成' if rows and counts['completed']==len(rows) else '处理中'
 presented['stages']=[]
 for i,n in enumerate(['分析','文案','配音','剪辑','检查']):
  values=[x['stages'][i]['status'] for x in rows];status='WAITING_USER' if 'WAITING_USER' in values else 'PROCESSING' if 'PROCESSING' in values else 'DONE' if values and all(v=='DONE' for v in values) else 'PENDING'
  presented['stages'].append(dict(name='内容' if n=='文案' else n,status=status,label={'WAITING_USER':'待确认' if n=='文案' and presented['content_counts']['pending'] else '需要处理','PROCESSING':'处理中','DONE':'已完成','PENDING':'未开始'}[status]))
 return presented

def invalidate_final(store,id,rank):
 with store.lock,store.connect() as db:
  db.execute("DELETE FROM checkpoints WHERE job_id=? AND item IN (?,?) AND stage IN ('FINAL_RENDERED','QA_DONE','FINAL_APPROVED')",(id,str(int(rank)),f'{int(rank):02}'))
