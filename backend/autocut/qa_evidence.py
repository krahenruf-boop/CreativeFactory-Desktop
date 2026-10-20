"""Show every montage cut to final QA without decoding or exporting again."""
import json
import math
from pathlib import Path
from PIL import Image, ImageDraw
from .ownership import checkpoints, digest
from .production_policy import approved_content
from app.s3.headless_media import FRACTIONS
from app.s3.headless_runtime import sha, write

VERSION = 'QA_EVIDENCE_COVERAGE_V1'


def review_images(work, rank, plan, technical):
    folder = Path(work) / 'qa' / str(rank)
    contact = folder / 'contact.jpg'
    if not contact.is_file():
        raise ValueError('QA_EVIDENCE_MISSING')
    images = [str(contact)]
    segments = plan.get('montage_segments') or []
    if not segments:
        return images, {}
    duration = float(technical.get('duration') or 0)
    samples = technical.get('samples') or []
    if not math.isfinite(duration) or duration <= 0 or not samples:
        raise ValueError('QA_EVIDENCE_MISSING')
    shown = {min(samples, key=lambda s: abs(s['time'] - duration * f))['frame']
             for f in FRACTIONS}
    required = []
    selected = {}
    artifacts = {str(Path(a['path']).resolve()): a['sha256']
                 for a in technical.get('artifacts', []) if a.get('path') and a.get('sha256')}
    for segment in segments:
        lo, hi = segment['output_start'], segment['output_end']
        if not (0 <= lo < hi <= duration + .034):
            raise ValueError('QA_EVIDENCE_INTERVAL_INVALID')
        for time in (lo, (lo + hi) / 2, max(lo, hi - 1 / 30)):
            sample = min(samples, key=lambda s: abs(s['time'] - time))
            if abs(sample['time'] - time) > 1 / 30 + .001:
                raise ValueError('QA_EVIDENCE_MISSING')
            path = Path(sample['path']).resolve()
            if not path.is_relative_to(folder.resolve()) or not path.is_file():
                raise ValueError('QA_EVIDENCE_MISSING')
            fingerprint = sha(path)
            if artifacts.get(str(path)) and artifacts[str(path)] != fingerprint:
                raise ValueError('QA_EVIDENCE_CHANGED')
            required.append(dict(requested_time=time, time=sample['time'],
                                 frame=sample['frame'], sha256=fingerprint))
            if sample['frame'] not in shown:
                selected[sample['frame']] = dict(sample, sha256=fingerprint)
    coverage = dict(contract=VERSION, output_sha256=technical['output_sha256'],
                    duration=duration, required_samples=required)
    key = digest(coverage)
    coverage['key'] = key
    extra = sorted(selected.values(), key=lambda s: s['time'])
    if extra:
        destination = folder / 'evidence_coverage' / key
        destination.mkdir(parents=True, exist_ok=True)
        for offset in range(0, len(extra), 9):
            page = extra[offset:offset + 9]
            canvas = Image.new('RGB', (810, math.ceil(len(page) / 3) * 510), '#eeeeee')
            draw = ImageDraw.Draw(canvas)
            for index, sample in enumerate(page):
                with Image.open(sample['path']) as frame:
                    canvas.paste(frame.convert('RGB').resize((270, 480)),
                                 ((index % 3) * 270, (index // 3) * 510))
                draw.text(((index % 3) * 270 + 8, (index // 3) * 510 + 484),
                          f"{sample['time']:.2f}s", fill='black')
            path = destination / f'page_{offset // 9 + 1:02}.jpg'
            canvas.save(path, quality=95)
            images.append(str(path))
    coverage['additional_images'] = [dict(path=p, sha256=sha(p)) for p in images[1:]]
    return images, coverage


def request(manager, ident, ranks):
    """One evidenced final-QA retry, retaining the failed verdict and MP4."""
    from .ai_recovery import final_evidence
    from .item_review import get_item, audit
    store = manager.store
    rows = []
    with store.lock, manager.lock:
        job = store.get(ident)
        if (job.get('deleted_at') or job.get('control_request')
                or job.get('state') in {'COMPLETED', 'PAUSED', 'STOPPED'}
                or (job.get('batch_review') or {}).get('phase') != 'RUNNING'):
            raise ValueError('STATE_TRANSITION_REJECTED')
        if not isinstance(ranks, list) or not ranks:
            raise ValueError('INVALID_REQUEST')
        ranks = [f'{int(rank):02}' for rank in ranks]
        if len(ranks) != len(set(ranks)):
            raise ValueError('INVALID_REQUEST')
        for rank in ranks:
            _, item = get_item(store, ident, rank)
            if item['status'] == 'PASS' or (ident, rank) in manager.item_pending:
                continue
            if (item['status'] != 'FAILED' or item.get('issues') != ['SEVERE_VISUAL_QA']
                    or not approved_content(item)):
                raise ValueError('STATE_TRANSITION_REJECTED')
            work, plan, rendered, technical, _ = final_evidence(store, job, item)
            for entry in [plan['source'], plan['audio'], *plan.get('montage_segments', [])]:
                path = entry.get('path') or entry.get('audio_path')
                if not path or sha(path) != entry.get('sha256'):
                    raise ValueError('RECOVERY_CACHE_INVALID')
            previous = checkpoints(store, ident, rank)
            old = previous.get('QA_DONE', {}).get('data', {})
            if old.get('status') != 'FAIL' or not old.get('visual'):
                raise ValueError('STATE_TRANSITION_REJECTED')
            # Preserve and validate hashes of the already decoded frame files.
            technical = dict(technical, artifacts=old.get('technical', {}).get('artifacts', []))
            _, coverage = review_images(work, rank, plan, technical)
            if not coverage.get('additional_images'):
                raise ValueError('QA_EVIDENCE_ALREADY_COVERED')
            prior = previous.get(VERSION, {}).get('data', {})
            if prior.get('coverage', {}).get('key') == coverage['key']:
                continue
            with store.connect() as db:
                row = db.execute("SELECT input_sha,data FROM checkpoints WHERE job_id=? AND stage='VISUAL_QA' AND item=?",
                                 (ident, 'final-' + rank)).fetchone()
            final_review = dict(input_sha=row['input_sha'], data=json.loads(row['data'])) if row else None
            record = dict(status='REQUESTED', output_sha256=technical['output_sha256'],
                          render_input_sha=rendered['input_sha'], coverage=coverage,
                          previous_qa=previous.get('QA_DONE'), previous_visual=final_review)
            rows.append((rank, work, record))
        for rank, work, record in rows:
            write(work / 'qa_evidence_recovery' / rank / (record['coverage']['key'] + '.json'), record)
            store.done(ident, VERSION, rank, record['output_sha256'], record)
            store.item(ident, rank, status='QUEUED', issues=[], resume_pending=True,
                       resume_phase='AI_FINAL_QA', qa_evidence_recovery=VERSION)
            audit(store, ident, rank, VERSION, render_reused=True, audio_reused=True,
                  output_sha256=record['output_sha256'])
        if rows:
            store.update(ident, state='RUNNING', stage='检查')
            manager.enqueue_batch(ident, [(rank, 'AI_FINAL_QA') for rank, _, _ in rows])
