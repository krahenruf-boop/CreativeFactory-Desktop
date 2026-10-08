"""Opt-in research overlay stage. Standard library only; never imports torch.

Output is the selected source interval, with its original audio packets copied.
The production renderer does not select this backend automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from fractions import Fraction


ROOT = Path(__file__).resolve().parents[2]
BACKEND = 'EXPERIMENTAL_HARD_INPAINT'
SCOPES = {'training_research', 'internal_training_research'}
CLASSES = {'STATIC_OVER_TEXTURE', 'HARD_STATIC_WATERMARK'}


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def command(args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, capture_output=True,
                          text=True, timeout=kwargs.pop('timeout', 120), **kwargs).stdout


def probe(path):
    return json.loads(command(['ffprobe', '-v', 'error', '-show_streams',
                              '-show_format', '-of', 'json', path]))


def audio_packets(path):
    data = json.loads(command(['ffprobe', '-v', 'error', '-select_streams', 'a',
                              '-show_packets', '-show_data_hash', 'sha256',
                              '-show_entries', 'packet=stream_index,pts_time,duration_time,data_hash',
                              '-of', 'json', path]))
    return data.get('packets', [])


def validate_spec(spec, allow_experimental=False):
    if spec.get('usage_scope') not in SCOPES:
        raise ValueError('RESEARCH_SCOPE_REQUIRED')
    if not (allow_experimental or (spec.get('backend') == 'propainter' and spec.get('experimental') is True)):
        raise ValueError('EXPERIMENTAL_BACKEND_DISABLED')
    if spec.get('device', 'mps') != 'mps':
        raise ValueError('MPS_REQUIRED')
    if spec.get('mask_mode', 'STATIC_BOX') != 'STATIC_BOX':
        raise ValueError('MOVING_MASK_REQUIRED')
    if spec.get('overlay_class') not in CLASSES or spec.get('lightweight_method_failed') is not True:
        raise ValueError('OVERLAY_NOT_ELIGIBLE')
    start, end = float(spec['start_time']), float(spec['end_time'])
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError('INVALID_INTERVAL')
    box = spec['bbox']
    if len(box) != 4 or any(type(v) is not int for v in box) or min(box[:2]) < 0 or min(box[2:]) <= 0:
        raise ValueError('INVALID_BBOX')
    if not 32 <= spec.get('context_pixels', 48) <= 96:
        raise ValueError('INVALID_CONTEXT')
    if not 0.25 <= spec.get('processing_scale', 1.0) <= 1:
        raise ValueError('INVALID_PROCESSING_SCALE')


class ProPainterBackend:
    enabled = False

    def __init__(self, project_root=ROOT):
        self.root = Path(project_root)
        self.python = self.root / '.venv-propainter/bin/python'
        self.cli = self.root / '.venv-propainter/bin/propainter-delogo'
        self.upstream = self.root / 'tools/third_party/propainter-delogo/third_party/ProPainter'

    def run(self, spec, *, allow_experimental=False):
        started = time.monotonic()
        result = dict(success=False, output_path=None, backend=BACKEND, device='mps',
                      inpainting_backend='propainter',
                      experimental=True, usage_scope='internal_training_research',
                      status='REVIEW_REQUIRED', retry_count=0, error=None)
        work = None
        try:
            validate_spec(spec, allow_experimental)
            source = Path(spec['input_video']).resolve(strict=True)
            output = Path(spec['output_path']).resolve()
            if source == output or output.exists():
                raise ValueError('OUTPUT_MUST_BE_NEW')
            source_hash = digest(source)
            mask = Path(spec['mask_path']).resolve(strict=True) if spec.get('mask_path') else None
            identity = dict(source_sha256=source_hash, bbox=spec['bbox'],
                            mask_sha256=digest(mask) if mask else None)
            result.update(identity)
            start, end = float(spec['start_time']), float(spec['end_time'])
            if end - start > 5.001:
                approval = json.loads(Path(spec.get('approved_preview', '')).read_text()) if spec.get('approved_preview') else {}
                if (not approval.get('success') or approval.get('visual_review_approved') is not True
                        or any(approval.get(k) != v for k, v in identity.items())):
                    raise ValueError('APPROVED_PREVIEW_REQUIRED')
            info = probe(source)
            video = next(s for s in info['streams'] if s['codec_type'] == 'video')
            x, y, w, h = spec['bbox']
            if x + w > video['width'] or y + h > video['height'] or w * h > video['width'] * video['height'] * .20:
                raise ValueError('BBOX_TOO_LARGE_OR_OUTSIDE_FRAME')
            if end > float(info['format']['duration']) + .001:
                raise ValueError('INTERVAL_OUT_OF_RANGE')
            # This check runs in the isolated interpreter before any inference.
            command([self.python, '-c', "import torch; assert torch.backends.mps.is_available(), 'MPS_UNAVAILABLE'; print(torch.ones(1,device='mps').sum().item())"])
            for name in ('ProPainter.pth', 'recurrent_flow_completion.pth', 'raft-things.pth'):
                if (self.upstream / 'weights' / name).stat().st_size < 1_000_000:
                    raise ValueError('WEIGHTS_INCOMPLETE')
            output.parent.mkdir(parents=True, exist_ok=True)
            work = output.parent / (output.stem + '.work')
            work.mkdir(exist_ok=False)
            clip = work / 'input.mp4'
            # An explicit output-side -ss 0 drops AAC priming packets. Omit it
            # at the source origin so sample timing is preserved exactly.
            seek = ['-ss', start] if start > 0 else []
            command(['ffmpeg', '-v', 'error', '-nostdin', '-i', source, *seek,
                     '-t', end - start, '-map', '0:v:0', '-map', '0:a?', '-c:v', 'libx264',
                     '-preset', 'fast', '-crf', '16', '-pix_fmt', 'yuv420p', '-c:a', 'copy', clip])
            partial = work / 'result.mp4'
            cfg = dict(input=str(clip), output=str(partial), box=spec['bbox'], propainter=str(self.upstream),
                       workdir=str(work / 'inference'), pad=spec.get('context_pixels', 48),
                       proc_scale=spec.get('processing_scale', 1.0), soften=0.5, mask_dilation=2,
                       feather=1, raft_iter=12, neighbor_length=6, ref_stride=12,
                       subvideo_length=30, flat_black=False, fp16=False, preset='fast', keep_temp=True)
            if mask:
                worker_spec = dict(config=cfg, mask_path=str(mask), result_path=str(work / 'worker.json'))
                spec_path = work / 'worker_spec.json'
                spec_path.write_text(json.dumps(worker_spec, indent=2))
                args = [self.python, self.root / 'tools/propainter/masked_worker.py', spec_path]
            else:
                args = [self.cli, 'run', '--input', clip, '--output', partial, '--box', *spec['bbox'],
                        '--propainter', self.upstream, '--workdir', work / 'inference', '--pad', cfg['pad'],
                        '--proc-scale', cfg['proc_scale'], '--soften', '.5', '--mask-dilation', '2',
                        '--feather', '1', '--raft-iter', '12', '--neighbor-length', '6', '--ref-stride', '12',
                        '--subvideo-length', '30', '--no-flat-black', '--no-fp16', '--preset', 'fast', '--keep-temp']
            log = output.with_suffix('.log')
            inference_start = time.monotonic()
            with log.open('w') as f:
                process = subprocess.Popen(list(map(str, args)), stdout=f, stderr=subprocess.STDOUT,
                                           start_new_session=True,
                                           env={**os.environ, 'PYTORCH_ENABLE_MPS_FALLBACK': '1'})
                try:
                    rc = process.wait(timeout=3600)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                    raise
                if rc:
                    raise RuntimeError(f'INFERENCE_FAILED exit={rc}; see {log}')
            result['processing_seconds'] = time.monotonic() - inference_start
            result['real_time_factor'] = result['processing_seconds'] / (end - start)
            final_info = probe(partial)
            final_v = next(s for s in final_info['streams'] if s['codec_type'] == 'video')
            if (final_v['width'], final_v['height']) != (video['width'], video['height']):
                raise ValueError('OUTPUT_DIMENSIONS_CHANGED')
            if abs(float(Fraction(final_v['avg_frame_rate'])) - float(Fraction(video['avg_frame_rate']))) > .01:
                raise ValueError('OUTPUT_FPS_CHANGED')
            if abs(float(final_v['duration']) - (end - start)) > .08:
                raise ValueError('OUTPUT_DURATION_MISMATCH')
            command(['ffmpeg', '-v', 'error', '-xerror', '-i', partial, '-f', 'null', '-'])
            a, b = audio_packets(clip), audio_packets(partial)
            if a != b:
                raise ValueError('AUDIO_PACKETS_OR_TIMESTAMPS_CHANGED')
            if digest(source) != source_hash:
                raise ValueError('SOURCE_HASH_CHANGED')
            partial.replace(output)
            result.update(success=True, status='PREVIEW_REVIEW' if end - start <= 5.001 else 'PROCESSED',
                          output_path=str(output), output_sha256=digest(output), source_hash_unchanged=True,
                          audio_preserved=True, audio_packet_count=len(a), decode=True,
                          start_time=start, end_time=end, duration=float(final_v['duration']))
            if (work / 'worker.json').exists():
                result['worker'] = json.loads((work / 'worker.json').read_text())
        except Exception as exc:
            result['error'] = 'PROPAINTER_FAILED'
            result['detail'] = str(exc)[-2000:]
            # No implicit retries, no whole-job CPU fallback, no original overwrite.
        finally:
            result['elapsed_seconds'] = time.monotonic() - started
            if work is not None:
                dest = Path(spec['output_path']).with_suffix('.manifest.json')
                dest.write_text(json.dumps(result, ensure_ascii=False, indent=2))
                if result['success']:
                    shutil.rmtree(work)
        return result


def prepare_transport_source(plan, workdir):
    """Optional hook for the existing folder Transport Harness, never auto-selected.

    Handles its single-primary interval only; leaves the original plan/source intact.
    The harness must stop this output on failure, rather than silently rendering it.
    """
    spec = plan.get('experimental_overlay')
    if spec is None:
        return None
    spec = {**spec, 'input_video': plan['source']['path'],
            'start_time': 0, 'end_time': plan['source']['source_end'],
            'output_path': str(Path(workdir) / 'propainter_source.mp4')}
    result = ProPainterBackend().run(spec)
    if not result['success']:
        raise RuntimeError(f"PROPAINTER_FAILED / REVIEW_REQUIRED: {result['detail']}")
    return result
