"""Serve checkout sources with the existing runtime; no App build or release.

Run with the system Python. --no-resume is for UI-only maintenance sessions.
The backend's normal per-data-directory lock prevents duplicate workers.
"""
import argparse
import json
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def engine_launchers(resources, *, retire_legacy=True):
    """Expose isolated Torch/MPS; ProPainter is no longer a selectable engine."""
    directory = resources / 'bin'
    directory.mkdir(exist_ok=True)
    prefix = ('#!/bin/sh\nset -eu\n'
              'autocut_resources="$(cd "$(dirname "$0")/.." && pwd)"\n'
              'export PYTHONDONTWRITEBYTECODE=1\n'
              'export PYTHONNOUSERSITE=1\n'
              'export PYTHONPATH="$autocut_resources/runtime/propainter"\n'
              'exec "$autocut_resources/runtime/python/bin/python3.12" ')
    names = ['mps-python']
    # An already-running older checker still uses this name for Torch only.
    # Retire it on the next normal startup; never restart active jobs for this.
    if not retire_legacy:
        names.append('propainter-python')
    for name in names:
        path = directory / name
        if path.is_symlink():
            raise SystemExit('Development engine launcher must not be a symlink.')
        content = prefix + '"$@"\n'
        if not path.exists() or path.read_text() != content:
            temporary = path.with_suffix('.tmp')
            if temporary.is_symlink():
                raise SystemExit('Development engine launcher temporary file must not be a symlink.')
            temporary.write_text(content)
            temporary.chmod(0o755)
            temporary.replace(path)
        path.chmod(0o755)
    for name in ['propainter-delogo'] + (['propainter-python'] if retire_legacy else []):
        (directory / name).unlink(missing_ok=True)


def browser_session(home, resources):
    """Keep the local Web session across dev-server restarts; never log it."""
    runtime = home / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = runtime / 'dev-session.token'
    if path.is_symlink():
        raise SystemExit('Development session file must not be a symlink.')
    if not path.exists():
        token = None
        try:
            previous = json.loads((runtime / 'backend.json').read_text())
            if previous.get('resources') == str(resources):
                token = previous.get('token')
        except (OSError, ValueError):
            pass
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{48}', token):
            token = secrets.token_urlsafe(36)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w') as stream:
                stream.write(token)
    if not re.fullmatch(r'[A-Za-z0-9_-]{48}', path.read_text()):
        raise SystemExit('Development session file is invalid; check runtime/dev-session.token.')
    path.chmod(0o600)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=52857)
    parser.add_argument('--no-resume', action='store_true', help='Do not resume existing jobs at startup')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be between 1024 and 65535')
    runtime = ROOT / 'staging/runtime'
    python = runtime / 'python/bin/python3.12'
    resources = ROOT / 'build/dev-resources'
    resources.mkdir(parents=True, exist_ok=True)
    for name, target in [('engine', ROOT / 'staging/engine'), ('runtime', runtime),
                         ('third_party', ROOT / 'staging/third_party')]:
        if not target.is_dir():
            raise SystemExit(f'Missing existing runtime resource: {target}')
        link = resources / name
        if not link.exists():
            link.symlink_to(target, target_is_directory=True)
    helper = ROOT / 'build/autocut-keychain'
    if not python.is_file() or not helper.is_file():
        raise SystemExit('Existing Python runtime or Keychain helper is missing; no build was started.')
    (resources / 'bin').mkdir(exist_ok=True)
    link = resources / 'bin/autocut-keychain'
    if not link.exists():
        link.symlink_to(helper)
    engine_launchers(resources)
    version = json.loads((ROOT / 'manifests/latest-build.json').read_text())
    version.update(version=(ROOT / 'AUTOCUT_VERSION').read_text().strip(), development=True,
                   propainter_enabled=False)
    (resources / 'version.json').write_text(json.dumps(version))
    env = dict(os.environ, AUTOCUT_RESOURCES=str(resources),
               PYTHONPATH=os.pathsep.join([str(ROOT / 'backend'), str(runtime / 'main')]),
               PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    home = Path(env.get('AUTOCUT_DATA_HOME', Path.home() / 'Library/Application Support/AutoCut')).resolve()
    session = browser_session(home, resources)
    code = ('from pathlib import Path; from autocut.server import main; '
            f'main(port={args.port}, web={str(ROOT / "web")!r}, recover_on_startup={not args.no_resume!r}, '
            f'session_token=Path({str(session)!r}).read_text(), browser_entry=True)')
    print(f'Creative Factory source server: http://127.0.0.1:{args.port} (no App build)', flush=True)
    os.execve(python, [str(python), '-B', '-c', code], env)


if __name__ == '__main__':
    main()
