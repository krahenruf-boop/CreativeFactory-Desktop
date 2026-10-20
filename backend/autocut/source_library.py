"""Content-addressed source inspection; exclusions never modify source files."""
import copy
import math
import re
import subprocess
from pathlib import Path
from app.s3.headless_runtime import read, write, sha, locked, fingerprint
from .parallel_tasks import resource_slot

POLICY = 'PRODUCTION_PREFLIGHT_V1'
VERSION = 'SOURCE_LIBRARY_V1'


def usable_intervals(duration, black, fps=30):
    """Only verified short edge blacks qualify. Interior/darker footage stays."""
    lo, hi = 0., duration
    for start, end in black:
        if start <= .0001 and 0 < end <= .5:
            lo = max(lo, math.ceil(end * fps - 1e-6) / fps)
        if end >= duration - 1 / fps - .0001 and 0 < duration - start <= .5:
            hi = min(hi, math.floor(start * fps + 1e-6) / fps)
    return [[lo, hi]] if hi - lo >= .5 else [[0., duration]]


def inspect_source(home, source, frame_mode='WIDTH_FIT_BLUR_V1'):
    inputs = [VERSION, source['sha256'], source['duration'], source['width'], source['height']]
    if frame_mode != 'WIDTH_FIT_BLUR_V1':inputs.append(frame_mode)
    key = fingerprint(inputs)
    path = Path(home) / 'cache/source-library' / (key + '.json')
    if sha(source['path']) != source['sha256']:
        raise ValueError('SOURCE_HASH_CHANGED')
    with locked(path.with_suffix('.lock')):
        cached = read(path)
        if cached and cached.get('status') == 'READY':
            return cached
        duration = float(source['duration'])
        record = dict(version=VERSION, source_sha256=source['sha256'], width=source['width'],
                      height=source['height'], duration=duration, status='READY',
                      black_intervals=[], usable_intervals=[[0., duration]])
        try:
            with resource_slot('cpu'):
                result = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'info', '-i', source['path'],
                    '-vf', 'blackdetect=d=0.01:pix_th=0.02:pic_th=1.0', '-an', '-f', 'null', '-'],
                    capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise ValueError('SOURCE_INSPECTION_FAILED')
            record['black_intervals'] = [[float(a), float(b)] for a, b in
                re.findall(r'black_start:([0-9.]+) black_end:([0-9.]+)', result.stderr)]
            record['usable_intervals'] = usable_intervals(duration, record['black_intervals'])
        except (OSError, ValueError, subprocess.SubprocessError):
            record.update(status='WARNING', issue='SOURCE_INSPECTION_UNAVAILABLE')
        if sha(source['path']) != source['sha256']:
            raise ValueError('SOURCE_HASH_CHANGED')
        record['effective_duration'] = sum(b - a for a, b in record['usable_intervals'])
        from .source_frame import geometry
        try:
            with resource_slot('cpu'):record['frame_layout']=geometry(source,1080,1920,frame_mode)
            record['frame_issue']=None
        except ValueError as error:
            if str(error)=='SOURCE_HASH_CHANGED':raise
            record['frame_issue']=str(error)
        write(path, record)
        return record


def trim_clips(clips, health=None):
    result = []
    for clip in clips:
        entry = (health or {}).get(clip['sha256']) or clip.get('source_health') or {}
        intervals = entry.get('usable_intervals') if entry.get('status') == 'READY' else None
        if not intervals:
            result.append(copy.deepcopy(clip))
            continue
        for a, b in intervals:
            lo, hi = max(a, clip['source_start']), min(b, clip['source_end'])
            if hi - lo >= .3:
                result.append(dict(clip, source_start=lo, source_end=hi, source_health=entry))
    if not result:
        raise ValueError('SOURCE_USABLE_INTERVAL_REQUIRED')
    return result


def adapt(source, clips):
    """Ensure the final builder consumes precisely the checked intervals."""
    if not source.get('source_health') and not any(c.get('source_health') for c in clips):
        return source
    return dict(copy.deepcopy(source), remix_clips=clips,
                primary_source=source.get('primary_source', source),
                duration=sum(c['source_end'] - c['source_start'] for c in clips),
                creative_angle=source.get('creative_angle', '完整保留画面，排除已核实的纯黑首尾'))
