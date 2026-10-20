"""Verified local originals for an existing Feishu intake, without cloud calls."""
import copy
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from ..file_storage import VIDEO_EXTENSIONS, sha256
from .storage import fingerprint

VERSION = 'LOCAL_FEISHU_MEDIA_V1'


def source_snapshot(task, path, source_hashes, authorize):
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValueError('FEISHU_TASK_PATH_UNSAFE')
    source = source.resolve(strict=True)
    authorize(source)
    if not source.is_dir():
        raise ValueError('SOURCE_NOT_FOLDER')
    expected = (task.get('media') or {}).get('source_files') or []
    names = [r.get('file_name', '') for r in expected]
    if (len(expected) != task['expected_count'] or len({n.casefold() for n in names}) != len(names)
            or any(not n or Path(n).name != n or n.startswith('.') or '\\' in n for n in names)):
        raise ValueError('FEISHU_TASK_LOCAL_MANIFEST_REQUIRED')
    actual = {p.name: p for p in source.iterdir()
              if not p.name.startswith('.') and p.suffix.lower() in VIDEO_EXTENSIONS}
    if set(actual) != set(names):
        raise ValueError('FEISHU_TASK_LOCAL_SOURCE_MISMATCH')
    if set(source_hashes) != set(names) or any(not re.fullmatch(r'[a-f0-9]{64}', v) for v in source_hashes.values()):
        raise ValueError('FEISHU_TASK_LOCAL_SOURCE_MISMATCH')
    files = []
    for row in expected:
        file = actual[row['file_name']]
        if file.is_symlink() or not file.is_file() or file.stat().st_size != int(row['size']):
            raise ValueError('FEISHU_TASK_LOCAL_SOURCE_MISMATCH')
        digest = sha256(file)
        if digest != source_hashes[file.name]:
            raise ValueError('SOURCE_HASH_CHANGED')
        cached = ((task.get('media') or {}).get('downloads') or {}).get(file.name, {})
        if cached.get('sha256') and cached['sha256'] != digest:
            raise ValueError('SOURCE_HASH_CHANGED')
        files.append(dict(name=file.name, size=file.stat().st_size, sha256=digest, fid=row['fid']))
    return dict(version=VERSION, path=str(source), files=files,
                digest=fingerprint([task['id'], str(source), files]))


def intake(task, root, checkpoint, stop=None):
    """Runs on the existing single Feishu transfer worker; retains originals."""
    from app.s3.headless_media import probe
    receipt = task['local_media']
    source = Path(receipt['path'])
    files = receipt['files']
    def check_stop():
        if stop and stop.is_set():
            raise ValueError('FEISHU_TASK_STOPPED')
    check_stop()
    if receipt.get('digest') != fingerprint([task['id'], str(source), files]):
        raise ValueError('SOURCE_HASH_CHANGED')
    key = task.get('intake_key') or task['id']
    if not re.fullmatch(r'CF-[0-9]{8}-[A-F0-9]{8}(?:-R[0-9]+)?', key):
        raise ValueError('FEISHU_TASK_PATH_UNSAFE')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    folder = root / key
    if folder.is_symlink():
        raise ValueError('FEISHU_TASK_PATH_UNSAFE')
    folder.mkdir(exist_ok=True)
    if folder.resolve().parent != root.resolve():
        raise ValueError('FEISHU_TASK_PATH_UNSAFE')
    # Verify the complete selection before touching any per-file checkpoint.
    for row in files:
        check_stop()
        path = source / row['name']
        if (source.is_symlink() or path.is_symlink() or not path.is_file()
                or path.stat().st_size != row['size'] or sha256(path) != row['sha256']):
            raise ValueError('SOURCE_HASH_CHANGED')
    media = copy.deepcopy(task.get('media') or {})
    completed = dict(media.get('downloads') or {})
    manifest = []
    for row in files:
        check_stop()
        target = folder / row['name']
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or sha256(target) != row['sha256']:
                raise ValueError('STORAGE_PATH_CONFLICT')
        else:
            temporary = folder / ('.local-' + uuid.uuid4().hex + '.part')
            shutil.copy2(source / row['name'], temporary)
            if temporary.stat().st_size != row['size'] or sha256(temporary) != row['sha256']:
                raise ValueError('SOURCE_HASH_CHANGED')
            os.replace(temporary, target)
        info = probe(target)
        video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), {})
        duration = float((info.get('format') or {}).get('duration') or video.get('duration') or 0)
        if not video.get('width') or not video.get('height') or not math.isfinite(duration) or duration <= 0:
            raise ValueError('QUARK_VIDEO_UNREADABLE')
        completed[row['name']] = dict(completed.get(row['name'], {}), sha256=row['sha256'], size=row['size'])
        media.update(downloads=completed, local_source=dict(version=VERSION, path=str(source), digest=receipt['digest']))
        checkpoint(media)
        manifest.append(dict(row, path=str(target), duration=duration, width=video['width'], height=video['height']))
    media['manifest'] = manifest
    check_stop()
    checkpoint(media)
    return folder, media
