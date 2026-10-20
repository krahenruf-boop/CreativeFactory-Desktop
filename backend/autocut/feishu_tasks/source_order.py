"""Keep the validated intake order stable across pipeline restarts."""
from pathlib import Path


def ordered_sources(sources, names):
    by_name = {row.get('filename') or Path(row['path']).name: row for row in sources}
    if len(set(names)) != len(names) or any(name not in by_name for name in names):
        raise ValueError('SOURCE_HASH_CHANGED')
    return [dict(by_name[name], rank=rank, filename=name) for rank, name in enumerate(names, 1)]
