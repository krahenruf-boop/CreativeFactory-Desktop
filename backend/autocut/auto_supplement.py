"""Append a small shortfall from validated sibling sources; no AI/provider calls."""
import copy
import math
from pathlib import Path
from .duration_fit import VOICE_SPEED_HARD_MAX
from .review_media import base_clips, duration_check, inspect_media
from .task_instructions import effective

VERSION = 'SAME_FOLDER_APPEND_V1'
MAX_MISSING_RATIO = 1 / 3
# A Remix already authorizes recombining validated product footage. Preserve
# at least half the original timeline; Transport keeps the smaller append cap.
MAX_REMIX_MISSING_RATIO = 1 / 2
FPS = 30


def frames(seconds):
    return math.floor(seconds * FPS + 1e-5)


def prepare(job, source, clips, voice_duration, task, music_only=False):
    """Pure planning. Only already analysed inputs in this Job/folder qualify.

    Measure the remaining deficit after allowed local voice speed adjustment.
    Keep the mode's bounded original-footage share and validate every donor.
    """
    fit = duration_check(voice_duration, clips, task, music_only)
    if fit['fits'] or music_only:
        return clips, fit, None
    available_frames = sum(frames(c['source_end'] - c['source_start']) for c in clips)
    required_frames = math.ceil(voice_duration / VOICE_SPEED_HARD_MAX * FPS - 1e-9)
    missing = required_frames - available_frames
    limit = MAX_REMIX_MISSING_RATIO if job.get('mode') == 'Remix' and source.get('remix_clips') else MAX_MISSING_RATIO
    record = dict(policy=VERSION, job_id=job['id'], item_id=f'{int(source["rank"]):02}',
        required_duration=required_frames / FPS, missing_duration=missing / FPS,
        missing_ratio=missing / required_frames, max_missing_ratio=limit,
        status='INSUFFICIENT', clips=[], skipped=[])
    if missing > required_frames * limit + 1e-9:
        record['status'] = 'GAP_TOO_LARGE'
        return clips, fit, record
    folder = Path(job['input_path']).resolve()
    if Path(source.get('primary_source', source)['path']).resolve().parent != folder:
        record['status'] = 'SOURCE_FOLDER_MISMATCH'
        return clips, fit, record
    used = {c['sha256'] for c in clips}; extra = []; remaining = missing
    for item in sorted(job.get('items', []), key=lambda x: int(x['rank'])):
        donor = item.get('source_snapshot') or {}; analysis = item.get('analysis') or {}
        if (item['rank'] == record['item_id'] or donor.get('status') != 'VALID'
                or not donor.get('remix_clips') and donor.get('sha256') in used
                or not analysis.get('product_description') or not donor.get('path')
                or Path(donor['path']).resolve().parent != folder):
            continue
        try:
            ranges = base_clips(donor, item.get('decision') or {}, effective(job, item['rank']))
            # Middle of a retained range avoids intro/outro cards; only take
            # enough footage to close the gap, never duplicate the current input.
            for interval in sorted(ranges, key=lambda c: c['source_end'] - c['source_start'], reverse=True):
                # Remix snapshots contain several original, analysed clips.
                # Validate the actual selected file rather than only the
                # montage's primary file; never repeat the current sources.
                if (interval.get('sha256') in used or not interval.get('path')
                        or Path(interval['path']).resolve().parent != folder):
                    continue
                media = inspect_media(interval['path'])
                health=interval.get('source_health') or donor.get('source_health') or {}
                if health.get('frame_issue') and interval['sha256'] not in (job.get('source_frame_exceptions') or {}):continue
                if media['sha256'] != interval['sha256']:
                    raise ValueError('SOURCE_HASH_CHANGED')
                if not 0 <= interval['source_start'] < interval['source_end'] <= media['duration'] + .001:
                    raise ValueError('SOURCE_INTERVAL_INVALID')
                lo = interval['source_start'] + .5
                hi = min(interval['source_end'], media['duration']) - .5
                capacity = frames(hi - lo)
                if capacity < 15:
                    continue
                take = min(max(15, remaining), capacity, 6 * FPS)
                start = math.ceil(((lo + hi - take / FPS) / 2) * FPS) / FPS
                if start + take / FPS > hi + 1e-6:
                    continue
                extra.append(dict(path=media['path'], sha256=media['sha256'],
                    source_start=start, source_end=start + take / FPS,
                    source_duration=media['duration'], source_rank=int(interval.get('source_rank') or item['rank']),
                    reason='同任务同文件夹产品画面，自动裁剪补在末尾'))
                used.add(media['sha256']); remaining -= take
                break
        except (OSError, ValueError) as error:
            record['skipped'].append(dict(rank=item['rank'], reason=str(error)))
        if remaining <= 0 or len(extra) >= 12:
            break
    if remaining > 0:
        record['remaining_duration'] = max(0, remaining / FPS)
        return clips, fit, record  # Never commit a partial/insufficient append.
    joined = copy.deepcopy(clips) + extra
    # Frame-align cuts without changing original order or speeding up scarce
    # footage. Quantizing each clip prevents losing frames at concat boundaries.
    for clip in joined:
        clip['source_end'] = clip['source_start'] + frames(clip['source_end'] - clip['source_start']) / FPS
    fit = duration_check(voice_duration, joined, dict(task, conservative_motion=True))
    if not fit['fits']:
        return clips, fit, record
    record.update(status='READY', clips=extra, added_duration=sum(c['source_end'] - c['source_start'] for c in extra),
        base_clip_count=len(clips), duration_match=fit)
    return joined, fit, record


def recover(manager, ident, ranks):
    """Recheck small duration gaps against validated same-job clips only."""
    from .item_review import get_item, audit
    from .production_policy import approved_content
    from .script_language import script, title, check, target_language
    from app.s3.headless_runtime import sha
    store = manager.store
    with store.lock, manager.lock:
        job = store.get(ident)
        if (job.get('deleted_at') or job.get('control_request') or manager.control.busy(ident)
                or (job.get('batch_review') or {}).get('phase') != 'RUNNING'):
            raise ValueError('STATE_TRANSITION_REJECTED')
        if not isinstance(ranks, list) or not ranks:
            raise ValueError('INVALID_REQUEST')
        normalized = [f'{int(rank):02}' for rank in ranks]
        if len(set(normalized)) != len(normalized):
            raise ValueError('INVALID_REQUEST')
        rows = []
        for rank in normalized:
            _, item = get_item(store, ident, rank)
            if item['status'] == 'PASS':
                continue
            if (item['status'] != 'WAITING_SUPPLEMENT_MEDIA'
                    or item.get('issues') != ['DURATION_FIT_FAILED']):
                raise ValueError('STATE_TRANSITION_REJECTED')
            if not approved_content(item):
                raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
            spoken = check(script(item), target_language(job))
            check(title(item), target_language(job), 'TITLE')
            source = item.get('source_snapshot') or {}
            audio = item.get('audio_result') or {}
            if (not item.get('analysis') or not item.get('decision') or item.get('plan')
                    or item.get('output') or audio.get('script_text') != spoken['text']):
                raise ValueError('RECOVERY_CACHE_INVALID')
            for cached in (source, audio):
                path = Path(cached.get('path', ''))
                if not path.is_file() or sha(path) != cached.get('sha256'):
                    raise ValueError('RECOVERY_CACHE_INVALID')
            task = dict(effective(job, rank), conservative_motion=bool(
                item['analysis'].get('talking_head') or item['analysis'].get('detailed_demonstration')))
            _, fit, record = prepare(job, source, base_clips(source, item['decision'], task),
                                     audio['duration'], task)
            if not fit['fits'] or not record or record['status'] != 'READY':
                raise ValueError('VISUAL_DURATION_INSUFFICIENT')
            rows.append((rank, record))
        # Validate every requested item before changing any job or queue state.
        for rank, record in rows:
            store.item(ident, rank, status='QUEUED', issues=[], resume_pending=True,
                       resume_phase='SCRIPT', same_folder_recovery=VERSION)
            audit(store, ident, rank, 'SAME_FOLDER_DURATION_RECOVERY', audio_reused=True,
                  script_preserved=True, title_preserved=True, clips=record['clips'])
        if rows:
            store.update(ident, state='RUNNING', stage='剪辑')
            manager.enqueue_batch(ident, [(rank, 'SCRIPT') for rank, _ in rows])


def adapt(source, clips, record):
    if not record or record['status'] != 'READY':
        return source
    return dict(copy.deepcopy(source), remix_clips=clips,
        duration=sum(c['source_end'] - c['source_start'] for c in clips),
        primary_source=source.get('primary_source', source), auto_supplement=record,
        creative_angle='保留原结构，末尾补充同文件夹画面')


def apply_montage(plan, source):
    """Keep original clips in order; append donor clips on the final frame grid."""
    left = round(plan['target_duration'] * FPS); cursor = 0; segments = []
    for clip in source['remix_clips']:
        n = min(frames(clip['source_end'] - clip['source_start']), left)
        if n <= 0:
            break
        segments.append(dict(clip, source_end=clip['source_start'] + n / FPS,
            speed=1., output_start=cursor / FPS, output_end=(cursor + n) / FPS))
        cursor += n; left -= n
    if left or len(segments) <= source['auto_supplement']['base_clip_count']:
        raise ValueError('AUTO_SUPPLEMENT_TIMELINE_INVALID')
    plan.pop('source_ranges', None)
    plan.update(source=dict(source['primary_source'], source_end=source['primary_source']['duration']),
        montage_segments=segments, video_speed=1., auto_supplement=source['auto_supplement'])
    from app.s3.headless_media import validate_montage
    validate_montage(plan)
