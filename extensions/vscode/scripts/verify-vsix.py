"""Verify target, native architecture, executable helpers and bundled hashes."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import struct
import xml.etree.ElementTree as ET
import zipfile

TARGETS = {'win32-x64', 'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64'}

def require(value, message):
    if not value:
        raise ValueError(message)

def binary_arch(data, target):
    os_name, arch = target.split('-')
    if os_name == 'win32':
        require(data[:2] == b'MZ' and len(data) >= 64, 'Expected a PE native binary')
        offset = struct.unpack_from('<I', data, 60)[0]
        require(data[offset:offset + 4] == b'PE\0\0', 'Invalid PE signature')
        require(struct.unpack_from('<H', data, offset + 4)[0] == 0x8664, 'Wrong PE architecture')
    elif os_name == 'linux':
        require(data[:4] == b'\x7fELF' and data[4:6] == b'\x02\x01', 'Expected a little-endian ELF64 binary')
        require(struct.unpack_from('<H', data, 18)[0] == {'x64':62, 'arm64':183}[arch], 'Wrong ELF architecture')
    else:
        require(data[:4] == b'\xcf\xfa\xed\xfe', 'Expected a thin little-endian Mach-O64 binary')
        require(struct.unpack_from('<I', data, 4)[0] == {'x64':0x01000007, 'arm64':0x0100000c}[arch], 'Wrong Mach-O architecture')

def verify(file, target=None, extract=None):
    with zipfile.ZipFile(file) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) <= 3000, 'Duplicate or excessive ZIP entries')
        require(sum(item.file_size for item in archive.infolist()) <= 100 * 1024 * 1024, 'VSIX exceeds verification limit')
        for name in names:
            path = PurePosixPath(name)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in name and ':' not in name, 'Unsafe ZIP path')
        require(archive.testzip() is None, 'ZIP checksum mismatch')
        identity = next(node for node in ET.fromstring(archive.read('extension.vsixmanifest')).iter() if node.tag.endswith('}Identity'))
        actual = identity.attrib.get('TargetPlatform')
        require(actual in TARGETS and (target is None or actual == target), 'VSIX target mismatch')
        package = json.loads(archive.read('extension/package.json'))
        require(identity.attrib['Version'] == package['version'], 'Version mismatch')
        require(package.get('icon') == 'media/symbol.png', 'Unexpected icon path')
        icon = archive.read('extension/' + package['icon'])
        require(hashlib.sha256(icon).hexdigest() == hashlib.sha256((Path(__file__).resolve().parents[1] / package['icon']).read_bytes()).hexdigest(), 'Icon source mismatch')
        native = json.loads(archive.read('extension/native-manifest.json'))
        require(native['schema'] == 1 and native['target'] == actual and native['node_pty'] == '1.1.0', 'Native manifest mismatch')
        prefix = 'node_modules/node-pty/prebuilds/' + actual + '/'
        expected = {'pty.node', 'conpty.node', 'conpty_console_list.node', 'winpty-agent.exe', 'winpty.dll', 'conpty/conpty.dll', 'conpty/OpenConsole.exe'} if actual.startswith('win32-') else {'pty.node', 'spawn-helper'} if actual.startswith('darwin-') else {'pty.node'}
        require({item['path'] for item in native['files']} == {prefix + name for name in expected}, 'Incomplete native inventory')
        for item in native['files']:
            name = 'extension/' + item['path']
            data = archive.read(name)
            require(hashlib.sha256(data).hexdigest() == item['sha256'], 'Native hash mismatch: ' + name)
            binary_arch(data, actual)
            if name.endswith('/spawn-helper'):
                require((archive.getinfo(name).external_attr >> 16) & 0o111, 'spawn-helper is not executable in the VSIX')
        packaged_native = {name.removeprefix('extension/') for name in names if name.startswith('extension/node_modules/node-pty/prebuilds/') and not name.endswith('/')}
        require(packaged_native == {item['path'] for item in native['files']}, 'Foreign or unlisted native assets')
        require(not any('/node-pty/build/' in name or name.endswith('.pdb') for name in names), 'Generated or debug native payload')
        runtime = json.loads(archive.read('extension/runtime-manifest.json'))
        require(runtime['extension_version'] == package['version'], 'Runtime version mismatch')
        for item in runtime['files']:
            require(hashlib.sha256(archive.read('extension/' + item['path'])).hexdigest() == item['sha256'], 'Runtime hash mismatch')
        require({item['path'] for item in runtime['files']} == {name.removeprefix('extension/') for name in names if name.startswith('extension/core/') and name.endswith('.py')}, 'Runtime inventory mismatch')
        if extract is not None:
            extract = Path(extract).absolute()
            require(not extract.is_symlink() and (not extract.exists() or not any(extract.iterdir())), 'Extraction requires a fresh directory')
            extract.mkdir(parents=True, exist_ok=True)
            archive.extractall(extract)
            if os.name == 'posix':
                for item in native['files']:
                    if item['path'].endswith('/spawn-helper'):
                        (extract / 'extension' / item['path']).chmod(0o755)
    return dict(passed=True, target=actual, version=package['version'], files=len(names), runtime_files=len(runtime['files']),
                native_files=len(native['files']), sha256=hashlib.sha256(Path(file).read_bytes()).hexdigest(),
                icon_sha256=hashlib.sha256(icon).hexdigest())

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vsix', type=Path)
    parser.add_argument('--target', choices=sorted(TARGETS))
    parser.add_argument('--extract', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.vsix, args.target, args.extract), indent=2))
    except (OSError, ValueError, KeyError, struct.error, zipfile.BadZipFile, StopIteration) as error:
        parser.exit(1, str(error) + '\n')

if __name__ == '__main__':
    main()
