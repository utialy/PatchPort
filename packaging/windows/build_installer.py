"""Build an offline installer only from a reviewed standalone desktop candidate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
from agent_bridge.install_manager import Package, file_table, relative_name
from agent_bridge.storage import checked_local_path


def payload(candidate, version, destination):
    candidate = checked_local_path(candidate)
    files = {}
    selected = []
    private = {'.git', '.env', '.aws', '.ssh', '.codex', '.claude', '.venv',
               'auth.json', 'credentials.json', '.bridge', 'projects.json'}
    for path in sorted(candidate.rglob('*')):
        checked_local_path(path, candidate)
        if not path.is_file():
            continue
        name = path.relative_to(candidate).as_posix()
        parts = relative_name(name).parts
        if '__pycache__' in parts:
            continue
        if any(p.lower() in private or p.lower().endswith(('.pem', '.key')) for p in parts):
            raise ValueError('Private file in candidate: ' + name)
        if parts[0] not in ('core', 'gui', 'notices', 'package.json'):
            raise ValueError('Unexpected candidate file: ' + name)
        data = path.read_bytes()
        files[name] = dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        selected.append((path, name))
    file_table(files)
    manifest = dict(schema=1, product='PatchPort', version=version, files=files)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, name in selected:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != files[name]['sha256']:
                raise ValueError('Candidate changed during build')
            archive.writestr(name, data)
        archive.writestr('install-manifest.json', json.dumps(manifest, sort_keys=True))
    package = Package(destination)
    return dict(version=version, package_sha256=package.digest, files=len(files),
                uncompressed_bytes=sum(i['size'] for i in files.values()))


def main():
    parser = argparse.ArgumentParser(description='Build a local Windows installer candidate')
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--payload-only', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    archive = output / 'payload.zip'
    report = payload(args.candidate.resolve(), args.version, archive)
    if not args.payload_only:
        command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                   '--windowed', '--name', 'PatchPortSetup', '--paths', str(REPO),
                   '--paths', str(REPO / 'src'), '--add-data', str(archive) + os.pathsep + 'payload',
                   '--distpath', str(output / 'dist'), '--workpath', str(output / 'work'),
                   '--specpath', str(output), str(REPO / 'desktop/install_launch.py')]
        with (output / 'build.log').open('wb') as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, cwd=REPO)
        executable = output / 'dist/PatchPortSetup.exe'
        report.update(executable=str(executable), exe_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
    (output / 'build-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
