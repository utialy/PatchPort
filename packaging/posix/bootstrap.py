"""Validate an offline distribution before importing its pure-Python wheel."""
import hashlib
import json
from pathlib import Path
import sys


def main():
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11+ with venv and ensurepip is required; no changes made')
    home = Path(__file__).resolve().parent
    manifest = json.loads((home / 'distribution.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or manifest.get('product') != 'PatchPort':
        raise ValueError('Invalid distribution manifest')
    for name in ('bootstrap.py', 'install.sh'):
        if hashlib.sha256((home / name).read_bytes()).hexdigest() != manifest['scripts'][name]:
            raise ValueError('Bootstrap checksum mismatch')
    name = manifest['wheel']
    if not isinstance(name, str) or '/' in name or '\\' in name or Path(name).name != name:
        raise ValueError('Invalid wheel filename')
    wheel = home / name
    if wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest() != manifest['sha256']:
        raise ValueError('Wheel checksum mismatch; no changes made')
    sys.path.insert(0, str(wheel))
    from agent_bridge.posix_install import main as install
    return install(distribution=dict(wheel=str(wheel), sha256=manifest['sha256'], build=manifest['build']))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps(dict(ok=False, error=str(exc))))
        raise SystemExit(1)
