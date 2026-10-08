"""Explicit production QA policy, never generic review/retry loops."""
VERSION='AUTO_PRODUCTION_V1'
# Only known ordinary warnings are auto-migrated. Unknown legacy errors stay hard.
WARNINGS={'BACKGROUND_BLUR','TEXT_GHOSTING','OVERLAY_BOUNDARY','CARD_STYLE','TEMPORAL_ARTIFACT','OVERLAY_REQUIRES_PREVIEW','VISUAL_REVIEW_REQUIRED','COPY_REVIEW_REQUIRED','CTA_SUBTITLE_COLLISION','CTA_TITLE_COLLISION','CTA_OUT_OF_CANVAS'}
HARD={'PROPAINTER_SUBJECT_DAMAGE','SUBJECT_DAMAGE','PRODUCT_OBSCURED','BLACK_SCREEN','NO_VIDEO','NO_AUDIO','AUDIO_MISSING','UNREADABLE_SUBTITLES','OUTPUT_CORRUPT','FULL_DECODE_FAILURE','SOURCE_HASH_CHANGED','FINAL_NOT_READY'}
RENDER_REPAIR={'FFMPEG_RENDER_FAILED','ENCODE_CORRUPTION','FULL_DECODE_FAILURE','FULL_DURATION_TIMING','PREFLIGHT_TECHNICAL_FAILED'}
LAYOUT_REPAIR={'CTA_OUT_OF_CANVAS','CTA_SUBTITLE_COLLISION','CTA_TITLE_COLLISION'}

def classify(result,technical=False):
 codes=[str(c) for c in result.get('issues',[])];error=result.get('error')
 if error:codes.append(error)
 # Legacy false flags remain blocking until re-reviewed with explicit severity.
 # A partial overlap is not evidence that the product is unusable.
 severity=result.get('severity')
 legacy_obstruction=result.get('product_preserved') is False and severity not in {'NONE','WARNING'}
 hard=bool(error) or result.get('status')=='FAIL' or severity in {'SEVERE','CRITICAL'} or legacy_obstruction or any(any(h in c.upper() for h in HARD) for c in codes)
 if technical and result.get('status',result.get('technical_status'))!='PASS':hard=True
 return dict(status='FAIL' if hard else 'WARNING' if codes or result.get('status') not in {'PASS',None} or any(result.get(k) is False for k in ('title','subtitle','cta','overlay','source_text','watermark')) else 'PASS',issues=codes,reason=result.get('reason',''),evidence=result,policy=VERSION)

def ordinary_legacy(x):
 codes=x.get('issues') or []
 return bool(codes) and all(c in WARNINGS for c in codes)

def approved_content(x):
 d=x.get('script_draft')
 return x.get('script_status')=='USER_APPROVED' and x.get('title_status')=='USER_APPROVED' and x.get('content_confirmed') and (not d or d.get('id')==x.get('approved_draft_id'))
