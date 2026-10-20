"""Prepare only the optional bot runtime and isolated Keychain helper, no App build."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    runtime = ROOT / 'staging/runtime'
    python = runtime / 'python/bin/python3.12'
    helper = ROOT / 'build/feishu-tasks-keychain'
    sdk = runtime / 'feishu-sdk'
    lock = ROOT / 'manifests/dependencies-feishu.json'
    if args.prepare:
        dependencies = json.loads(lock.read_text())['packages']
        subprocess.run([str(python), '-m', 'pip', 'install', '--target', str(sdk), '--upgrade',
                        '--disable-pip-version-check', *[name + '==' + version for name, version in dependencies.items()]], check=True)
        subprocess.run(['swiftc', str(ROOT / 'scripts/feishu_tasks_keychain.swift'), '-o', str(helper)], check=True)
        link = ROOT / 'build/dev-resources/bin/feishu-tasks-keychain'
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(helper)
    checks = dict(python=python.is_file(), feishu_sdk=(sdk / 'lark_oapi/__init__.py').is_file(),
                  isolated_keychain=helper.is_file(),
                  quark_cli=(Path.home() / '.agents/skills/quarkclouddrive/scripts/quark-drive.cjs').is_file())
    print(json.dumps(checks, ensure_ascii=False))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
