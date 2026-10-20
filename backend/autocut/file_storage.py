"""Persistent media locations; copying never changes the selected originals."""
import hashlib
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

_copy_lock = threading.RLock()
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm'}


def storage_root():
    return Path.home() / 'Movies' / 'Creative Factory'


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def copy_verified(source, destination):
    source, destination = Path(source), Path(destination)
    with _copy_lock:
        before = source.stat()
        digest = sha256(source)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.is_symlink():
            raise ValueError('STORAGE_PATH_CONFLICT')
        if destination.exists():
            if destination.stat().st_size != before.st_size or sha256(destination) != digest:
                raise ValueError('STORAGE_PATH_CONFLICT')
        else:
            temporary = destination.with_name('.copying-' + uuid.uuid4().hex)
            shutil.copy2(source, temporary)
            if temporary.stat().st_size != before.st_size or sha256(temporary) != digest:
                raise ValueError('STORAGE_COPY_FAILED')
            if source.stat().st_mtime_ns != before.st_mtime_ns or sha256(source) != digest:
                raise ValueError('SOURCE_HASH_CHANGED')
            if destination.exists():
                raise ValueError('STORAGE_PATH_CONFLICT')
            os.replace(temporary, destination)
        return dict(path=str(destination), sha256=digest, size=before.st_size)


def output_directory(job):
    source = job.get('original_input_path') or job.get('input_path')
    explicit = job.get('output_dir') or job.get('output_directory')
    if explicit:
        destination = Path(explicit).expanduser().resolve()
        if job.get('storage_policy') == 'source-v1':
            if not source:
                raise ValueError('OUTPUT_SOURCE_REQUIRED')
            original = Path(source).expanduser().resolve()
            if destination == original or not destination.is_relative_to(original):
                raise ValueError('OUTPUT_OUTSIDE_SOURCE')
        return destination
    if not source:
        raise ValueError('OUTPUT_SOURCE_REQUIRED')
    name = re.sub(r'[/\\\x00-\x1f]', '_', job.get('job_name') or job.get('product_name') or 'Creative Factory')[:100]
    return Path(source).expanduser().resolve() / 'outputs' / (name + '__' + job['id'][:8])


def supplement_directory(job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('JOB_PATH_UNSAFE')
    folder = storage_root() / 'Supplements' / job_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def preserve_job_sources(store, job, check=lambda: None):
    """Only new jobs opt in. Historical resume keeps its valid manifest."""
    if job.get('storage_policy') != 'fixed-v1' or job.get('sources_preserved'):
        return job
    source = Path(job['input_path'])
    destination = storage_root() / 'Media' / job['id']
    names = job.get('source_names')
    files = sorted(p for p in source.iterdir() if p.is_file() and not p.name.startswith('.')
                   and p.suffix.lower() in VIDEO_EXTENSIONS and (not names or p.name in names))
    if not files:
        raise ValueError('NO_VALID_SOURCE')
    records = []
    for path in files:
        check()
        records.append(copy_verified(path, destination / path.name))
    check()
    store.update(job['id'], input_path=str(destination), original_input_path=job.get('original_input_path') or str(source), sources_preserved=True,
                 source_intake=records, output_dir=str(output_directory(job)))
    return store.get(job['id'])
