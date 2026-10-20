"""Open recorded exports on the local Mac without accepting arbitrary paths."""
import subprocess
import sys
from pathlib import Path


def open_output(store, job_id, rank=None):
    job = store.get(job_id)
    rows = job['items']
    if rank is not None:
        rows = [x for x in rows if x['rank'] == rank]
    paths = [Path(x['output']).expanduser().resolve() for x in rows if x.get('output')]
    paths = [p for p in paths if p.is_file() and p.suffix.lower() == '.mp4']
    if not paths:
        raise ValueError('OUTPUT_NOT_FOUND')
    target = paths[0] if rank is not None else paths[0].parent
    if sys.platform != 'darwin':
        raise ValueError('OUTPUT_OPEN_UNAVAILABLE')
    try:
        subprocess.run(['/usr/bin/open', *(['-R'] if rank is not None else []), str(target)],
                       check=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('OUTPUT_OPEN_FAILED') from exc
    return {'status': 'OPENED', 'path': str(target)}
