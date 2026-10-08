"""Assemble a reviewable offline shell/bootstrap/wheel distribution."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
from agent_bridge.posix_install import version_key, wheel_info


def build(wheel, build_id, destination):
    version_key(build_id)
    wheel = wheel.resolve()
    sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel_info(wheel, sha256)
    files = {name: (Path(__file__).parent / name).read_text(encoding='utf-8').replace('\r\n', '\n').encode()
             for name in ('install.sh', 'bootstrap.py', 'README.md')}
    files['SERVICES.md'] = (REPO / 'packaging/services/README.md').read_text(encoding='utf-8').replace('\r\n', '\n').encode()
    files['MACOS.md'] = (REPO / 'packaging/services/MACOS.md').read_text(encoding='utf-8').replace('\r\n', '\n').encode()
    files['README.md'] = files['README.md'].replace(b'(../services/README.md)', b'(SERVICES.md)')
    files[wheel.name] = wheel.read_bytes()
    manifest = dict(schema=1, product='PatchPort', build=build_id, wheel=wheel.name, sha256=sha256,
                    scripts={name: hashlib.sha256(files[name]).hexdigest() for name in ('install.sh', 'bootstrap.py')})
    files['distribution.json'] = json.dumps(manifest, indent=2).encode()
    with tarfile.open(destination, 'x:gz', format=tarfile.PAX_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo('patchport-' + build_id + '/' + name)
            info.size = len(data)
            info.mode = 0o755 if name == 'install.sh' else 0o644
            archive.addfile(info, io.BytesIO(data))
    return dict(path=str(destination), sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                wheel_sha256=sha256, build=build_id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build a PatchPort offline POSIX bundle')
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--build-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.wheel, args.build_id, args.output.resolve()), indent=2))
