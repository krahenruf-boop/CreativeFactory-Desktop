"""Shared read-only review projections and a guard before any new paid TTS."""
import copy
import json
import math
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from app.s3.headless_runtime import read, sha, fingerprint
from app.s2.tts_text import tts_request_payload
from .source_library import POLICY
from .task_instructions import effective
from .duration_fit import VOICE_SPEED_HARD_MAX

COPY_POLICY = ('PRESERVE_SCRIPT_FIRST_V1: Preserve the complete source or selected manuscript and its factual claims. '
               'The target duration describes available footage, not permission to omit copy. '
               'Evaluate supplemental footage separately. Do not shorten to fit duration unless the real user explicitly requests SHORTEN.')

MESSAGES = {'TITLE_SHORTEN_REQUIRED': '标题过长，请编辑或另选能放下两行的标题',
            'TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED': '白底字幕区域尚不能安全覆盖，请核对原素材及覆盖位置',
            'TITLE_STICKER_LAYOUT_REQUIRED': '标题没有可避开贴图和产品的位置，请调整标题或复核素材',
            'SYMBOL_GLYPH_UNAVAILABLE': '标题中有当前字体无法显示的字符，请编辑后重新检查',
            'TITLE_LAYOUT_UNAVAILABLE': '标题排版暂无法核实，请稍后重试',
            'DURATION_PREFLIGHT_MEDIA_REQUIRED': '完整口播预计超出可用画面，请先补充同任务画面',
            'SOURCE_USABLE_INTERVAL_REQUIRED': '没有足够的有效画面，请补充素材'}


def enabled(job, item=None):
    return job.get('production_preflight_policy') == POLICY or bool((item or {}).get('title_confirmation_recovery'))


# A list projection checks three candidates per item. Keep the bounded cache
# above a normal multi-job history so polling does not evict its own results.
@lru_cache(maxsize=4096)
def _title(text, task_json):
    from app.s3.visual_preset import build_visual_plan
    from .title_layout import apply
    task = json.loads(task_json)
    try:
        visual = build_visual_plan(5, text, [])
        if task.get('title_position_adjustment'):
            # Position is validated again by the full plan; fit uses the exact
            # preset font, stroke, width and native symbol fallback.
            pass
        fit = apply(visual, text, max_lines=2)
        return dict(status='PASS', lines=fit['lines'], font_size=fit['reference_font_size'], message='两行排版可用')
    except (ValueError, OSError, RuntimeError) as error:
        code = str(error) if str(error) in MESSAGES else 'TITLE_LAYOUT_UNAVAILABLE'
        return dict(status='BLOCKED', error=code, message=MESSAGES[code])


def title_check(job, item, text):
    if not enabled(job, item) or not effective(job, item['rank']).get('title_enabled', True):
        return dict(status='NOT_APPLICABLE')
    task=effective(job,item['rank'])
    result=copy.deepcopy(_title(text,json.dumps(task,sort_keys=True)))
    if result['status']=='PASS':
        from .source_title_overlay import project_title
        try:
            cover=project_title(job,item,text,task)
            if cover:
                result.update(source_white_card=cover)
                if cover.get('warning'):
                    result.update(overlay_warning=cover['warning'],message='两行排版可用；白底覆盖按素材处理，保留成片检查')
                else:result['message']='两行标题可放入原白底字幕区域'
        except (ValueError,KeyError,TypeError) as error:
            code=str(error)
            code=code if code in MESSAGES else 'TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED'
            result.update(overlay_warning=code,message='两行排版可用；白底覆盖按素材处理，保留成片检查')
    return result


def require_title(job, item, text):
    result = title_check(job, item, text)
    if result['status'] == 'BLOCKED':
        raise ValueError(result['error'])
    return result


def cohort(payload, scope, language=None):
    from .script_language import canonical
    return fingerprint([scope, canonical(language or payload.get('language')), payload.get('voice_id'), payload.get('model_id'),
                        payload.get('settings'), payload.get('output_format'),
                        payload.get('previous_text'), payload.get('next_text')])


def words(text):
    return len(re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text, re.UNICODE))


def estimate(payload, scope, receipts, language=None):
    key = fingerprint(dict(provider='elevenlabs', scope=scope, kind='tts',
        payload=tts_request_payload(payload), schema='s2-v1', generation=None))
    for row in receipts:
        if row.get('fingerprint') == key and row.get('scope') == scope:
            try:
                if row.get('technical_status') == 'PASS' and sha(row['path']) == row['sha256']:
                    return dict(basis='EXISTING_AUDIO', reliable=True, lower=row['duration'], upper=row['duration'], samples=1)
            except (OSError, KeyError):
                pass
            return dict(basis='CACHE_REQUIRES_REVIEW', reliable=False, lower=None, upper=None, samples=0)
    samples = {}; target = cohort(payload, scope, language); length = words(payload.get('tts_text') or payload['text'])
    for row in receipts:
        p = row.get('payload') or {}; n = words(p.get('tts_text') or p.get('text', ''))
        duration = row.get('duration')
        if row.get('technical_status') == 'PASS' and n and type(duration) in (int, float) and math.isfinite(duration) and duration > 0 and cohort(p, row.get('scope'),row.get('script_language') or row.get('calibration_language')) == target:
            samples[row.get('fingerprint') or row.get('sha256')] = (n, duration / n)
    rows = list(samples.values()); count = len(rows)
    if count < 20 or not length or not min(n for n, _ in rows) <= length <= max(n for n, _ in rows):
        return dict(basis='INSUFFICIENT_CALIBRATION', reliable=False, lower=None, upper=None, samples=count)
    rates = [r for _, r in rows]
    # Conservative empirical envelope, checked on held-out receipts. Duplicated
    # output ranks sharing one paid request never inflate calibration support.
    coverage = sum(min(rates[:i] + rates[i+1:]) <= r <= max(rates[:i] + rates[i+1:]) for i, r in enumerate(rates)) / count
    return dict(basis='CALIBRATED_ESTIMATE', reliable=coverage >= .9,
                lower=length * min(rates), upper=length * max(rates), samples=count, coverage=coverage)


def receipts(home):
    return _receipts(str(home),int(time.monotonic()/5))


@lru_cache(maxsize=4)
def _receipts(home, bucket):
    result = []
    for path in (Path(home) / 'cache/tts').glob('*.result.json'):
        try:
            row = read(path)
            if isinstance(row, dict):result.append(row)
        except (OSError, ValueError):pass
    # Some provider models leave their language selector empty. Use the job's
    # confirmed language, never combine Portuguese and Spanish by voice alone.
    path=Path(home)/'autocut.sqlite3';languages={}
    if path.is_file():
        try:
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                for raw, in db.execute('SELECT data FROM job_items'):
                    item=json.loads(raw);audio=item.get('audio_result') or {};language=audio.get('script_language') or item.get('final_script_language')
                    if audio.get('fingerprint') and language:languages.setdefault(audio['fingerprint'],set()).add(language)
        except (sqlite3.Error,ValueError,TypeError):pass
    for row in result:
        known=languages.get(row.get('fingerprint'),set())
        if len(known)==1:row['calibration_language']=next(iter(known))
    return result


def select_voice(product, analysis, catalog):
    preferred = product.get('voice_preferences', {}).get(analysis.get('speaker', 'female'))
    voices = catalog.get('voices', [])
    return next((v for v in voices if v['id'] == preferred), None) or next((v for name in
        (['Lucas', 'Victor Pinho'] if analysis.get('speaker') == 'male' else ['Carla'])
        for v in voices if name.casefold() in v['name'].casefold()), None)


def duration_view(job, item, payload, scope, saved, clips=None, verify=False):
    from .review_media import base_clips, duration_check
    from .auto_supplement import prepare, MAX_MISSING_RATIO, MAX_REMIX_MISSING_RATIO
    source = item.get('source_snapshot') or {}
    if not source or not enabled(job) or item.get('voiceover_mode') == 'MUSIC_ONLY':return dict(status='NOT_APPLICABLE')
    task = effective(job, item['rank']); clips = clips or base_clips(source, item.get('decision') or {}, task)
    from .script_language import target_language
    language=target_language(job) if job.get('market') or job.get('target_language') else payload.get('language')
    result = estimate(payload, scope, saved, language)
    total = sum(c['source_end'] - c['source_start'] for c in clips)
    result.update(status='WARNING', available_seconds=total, message='口播时长暂不能可靠估算，制作时仍按实际音轨检查')
    if result['lower'] is None or not result['reliable']:return result
    low = duration_check(result['lower'], clips, task)
    result.update(duration_lower=result['lower'], duration_upper=result['upper'], status='READY', message='画面时长预检查可用')
    if low['fits']:return result
    if verify:
        _, fitted, append = prepare(job, source, clips, result['lower'], task)
        enough = fitted['fits']
    else:
        limit = MAX_REMIX_MISSING_RATIO if job.get('mode') == 'Remix' and source.get('remix_clips') else MAX_MISSING_RATIO
        required = result['lower'] / VOICE_SPEED_HARD_MAX
        used = {c['sha256'] for c in clips}; capacity = 0.; seen = set(used)
        for sibling in job.get('items', []):
            if not (sibling.get('analysis') or {}).get('product_description'):continue
            donor = sibling.get('source_snapshot') or {}
            if not donor:continue
            for clip in base_clips(donor, sibling.get('decision') or {}, effective(job, sibling['rank'])):
                health=clip.get('source_health') or donor.get('source_health') or {}
                if clip['sha256'] in seen or health.get('frame_issue') and clip['sha256'] not in (job.get('source_frame_exceptions') or {}):continue
                seen.add(clip['sha256']);capacity += min(6., max(0., clip['source_end']-clip['source_start']-1.))
        enough = required-total <= required*limit+1e-6 and total+capacity >= required
    if enough:
        result.update(status='WARNING', message='预计需要补充同任务画面，配音后仍核对实际时长')
    else:
        result.update(status='WAITING_MEDIA', error='DURATION_PREFLIGHT_MEDIA_REQUIRED',
                      missing_seconds=max(0., result['lower']/VOICE_SPEED_HARD_MAX-total),
                      message=MESSAGES['DURATION_PREFLIGHT_MEDIA_REQUIRED'])
    return result


def review_duration(home, job, item, content):
    if not enabled(job) or content.get('voiceover_mode') == 'MUSIC_ONLY':return dict(status='NOT_APPLICABLE')
    from .provider_models import tts_payload, job_models
    from .script_language import target_language
    try:
        cat = read(Path(home)/'cache/tts/catalog.json', {})
        voice = select_voice(job.get('product_profile') or {}, item.get('analysis') or {}, cat)
        if not voice:return dict(status='WARNING', message='音色准备后检查口播时长')
        text = (content.get('script') or {}).get('text', '')
        if not text:return dict(status='NOT_APPLICABLE')
        payload, _ = tts_payload(text, voice['id'], job_models(job)['elevenlabs'], cat, target_language(job))
        return duration_view(job, item, payload, cat['scope'], receipts(home))
    except (OSError, ValueError, KeyError, TypeError):
        return dict(status='WARNING', message='口播时长暂不能可靠估算，制作时仍按实际音轨检查')


def before_tts(store, job, item, payload, scope, saved, clips):
    if not enabled(job):return True
    from .source_frame import geometry, POLICY as FRAME_POLICY
    from .parallel_tasks import resource_slot
    try:
        with resource_slot('cpu'):
            for clip in clips:
                geometry(clip,1080,1920,(job.get('source_frame_exceptions') or {}).get(clip['sha256'],job.get('source_frame_mode') or FRAME_POLICY))
    except ValueError as error:
        store.item(job['id'],item['rank'],status='REVIEW_REQUIRED',issues=[str(error)],resume_pending=False)
        return False
    result = duration_view(job, item, payload, scope, saved, clips, verify=True)
    store.item(job['id'], item['rank'], production_preflight=result)
    if result['status'] == 'WAITING_MEDIA':
        store.item(job['id'], item['rank'], status='WAITING_SUPPLEMENT_MEDIA',
                   issues=['DURATION_PREFLIGHT_MEDIA_REQUIRED'], resume_pending=False,
                   duration_match=dict(missing_duration=result['missing_seconds'], available_visual_duration=result['available_seconds']))
        return False
    return True
