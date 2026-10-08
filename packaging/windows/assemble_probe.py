"""Assemble the isolated core for the desktop packaging experiment."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

RUNTIME_URL = 'https://www.python.org/ftp/python/3.14.6/python-3.14.6-embed-amd64.zip'
RUNTIME_SHA256 = 'df901e84a896ff1ee720ad03377e0c8d8c2244fda79808aeeaff6316df1cb75c'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def entries(archive):
    seen = set()
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        if (path.is_absolute() or '..' in path.parts or '\\' in item.orig_filename
                or ':' in item.filename or item.filename.casefold() in seen
                or (item.external_attr >> 16) & 0o170000 == 0o120000):
            raise ValueError('Unsafe archive entry: ' + item.filename)
        seen.add(item.filename.casefold())
        yield item


def assemble(runtime, wheel, output):
    if digest(runtime) != RUNTIME_SHA256:
        raise ValueError('Official runtime SHA-256 mismatch')
    if output.exists():
        raise ValueError('Output exists; choose a new experiment directory')
    with zipfile.ZipFile(runtime) as archive:
        runtime_items = list(entries(archive))
    with zipfile.ZipFile(wheel) as archive:
        wheel_items = list(entries(archive))
        names = {item.filename for item in wheel_items}
        required = {'agent_bridge/runner_manager.py', 'agent_bridge/setup_project.py',
                    'agent_bridge/_connect_assets/tools/templates/project_launcher.py.in'}
        if not required.issubset(names):
            raise ValueError('Wheel is missing required runtime or connection assets')
    core = output / 'core'
    core.mkdir(parents=True)
    with zipfile.ZipFile(runtime) as archive:
        archive.extractall(core, members=runtime_items)
    with zipfile.ZipFile(wheel) as archive:
        for item in wheel_items:
            if item.filename.startswith('agent_bridge/'):
                archive.extract(item, core / 'Lib')
    # Ignore PATH, PYTHONPATH, user site packages, and registry Python settings.
    (core / 'python314._pth').write_text('python314.zip\n.\nLib\n', encoding='ascii')
    manifest = dict(schema=1, experiment=True, runtime_url=RUNTIME_URL,
                    runtime_sha256=RUNTIME_SHA256, wheel_sha256=digest(wheel),
                    core_python='core/python.exe', gui='gui/PatchPortProbe.exe')
    (output / 'package.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    notices = output / 'notices'
    notices.mkdir()
    shutil.copy2(core / 'LICENSE.txt', notices / 'PYTHON-LICENSE.txt')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--wheel', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(assemble(args.runtime, args.wheel, args.output)))
