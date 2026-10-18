"""Check public repository content without modifying files."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import struct
import zlib

ROOT_FILES = {'.gitignore', '.gitattributes', 'README.md',
              'CONTRIBUTING.md', 'LICENSE', 'pyproject.toml', 'MANIFEST.in', 'CHANGELOG.md', 'setup.py'}
ROOT_DIRS = {'src', 'tests', 'tools', 'scripts', 'skills', 'examples', 'docs', '.github', 'extensions'}
EXCLUDED = {'.git', '.venv', '__pycache__', '.bridge', '.agent-bridge', '.role-flows',
            '.bridge-requests', '.bridge-integration-backups', 'workspaces', 'build', 'dist', 'node_modules'}
REQUIRED = {'README.md', 'LICENSE', 'pyproject.toml', 'CONTRIBUTING.md',
            'scripts/check_public_release.py'}
MAINTAINER_ONLY = {'docs/COMMIT_RULES.md', 'docs/MAINTAINER_UPDATES.md'}
TEXT_SUFFIXES = {'.py', '.md', '.toml', '.json', '.yml', '.yaml', '.in', '.ts', '.cjs', '.ps1', '.css', '.svg', '.sh', '.txt'}
TOKENS = [re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
          re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}'),
          re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}')]
LOCAL_PATH = re.compile(r'(?:[A-Za-z]:[/\\](?:Users|!Projects)[/\\]|/(?:home|Users)/[A-Za-z0-9_.-]+/)', re.I)


def check_png(data, *, icon=False):
    """Accept bounded screenshots without text or EXIF metadata."""
    if len(data) > 5 * 1024 * 1024 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return ['screenshot must be a PNG of at most 5 MiB']
    # Metadata exceptions apply only to exact, explicitly reviewed icon bytes.
    reviewed_metadata = {
        'c0ca1011192f4ce832d333c6530ebf4d17917c1632205f658a7d89691273b031': {b'caBX'},
        'cc79a3e83e0ae79e7d4b2c9f225d064df87c3b00832aa2d989d79c4f8c046fc5': {b'bKGD', b'tIME'},
    }.get(hashlib.sha256(data).hexdigest(), set()) if icon else set()
    offset, chunks = 8, []
    try:
        while offset < len(data):
            length = struct.unpack_from('>I', data, offset)[0]
            kind = data[offset + 4:offset + 8]
            end = offset + 12 + length
            allowed = (b'IHDR', b'IDAT', b'IEND', b'sRGB', b'gAMA', b'pHYs', b'cHRM')
            if end > len(data) or kind not in allowed and kind not in reviewed_metadata:
                return ['invalid PNG chunk or unreviewed screenshot metadata']
            if zlib.crc32(data[offset + 4:end - 4]) & 0xffffffff != struct.unpack_from('>I', data, end - 4)[0]:
                return ['PNG checksum mismatch']
            if kind == b'IHDR':
                width, height = struct.unpack_from('>II', data, offset + 8)
                if length != 13 or not 1 <= width <= 4096 or not 1 <= height <= 4096:
                    return ['screenshot dimensions exceed the reviewed limit']
            chunks.append(kind)
            offset = end
        if not chunks or chunks[0] != b'IHDR' or chunks[-1] != b'IEND' or b'IDAT' not in chunks:
            return ['incomplete PNG screenshot']
    except struct.error:
        return ['invalid PNG screenshot']
    return []


def check_content(name, data):
    errors = []
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or '..' in path.parts
            or (len(path.parts) == 1 and name not in ROOT_FILES)
            or (len(path.parts) > 1 and path.parts[0] not in ROOT_DIRS)):
        errors.append('path is outside the public file layout')
    if name.startswith(('desktop/', 'packaging/')) or name in ('src/agent_bridge/install_manager.py', 'src/agent_bridge/posix_install.py'):
        errors.append('standalone desktop/installer code is outside the extension distribution')
    if name in MAINTAINER_ONLY:
        errors.append('internal maintainer workflow is outside the public distribution')
    if (any(p in EXCLUDED or p.endswith('.egg-info') for p in path.parts)
            or name.startswith('extensions/vscode/core/') or name == 'extensions/vscode/runtime-manifest.json'
            or path.name.startswith(('.env', 'HANDOFF')) or path.name == 'NEXT_SESSION.md'
            or path.suffix in {'.pyc', '.pyo', '.pyd', '.db', '.sqlite3', '.log'}):
        errors.append('private or generated file is not allowed')
    if re.fullmatch(r'docs/images/[a-z0-9-]+\.png', name) or name == 'extensions/vscode/media/symbol.png':
        return errors + check_png(data, icon=name == 'extensions/vscode/media/symbol.png')
    if name.endswith(('/.gitignore', '/.vscodeignore', '/LICENSE')):
        pass
    elif name not in ROOT_FILES and path.suffix not in TEXT_SUFFIXES:
        errors.append('unsupported file type; review the policy before adding it')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return errors + ['content must be UTF-8 text']
    if not text.isascii():
        errors.append('use English ASCII text; preserve Unicode fixtures with escapes')
    if '\x00' in text:
        errors.append('binary content is not allowed')
    if any(pattern.search(data) for pattern in TOKENS):
        errors.append('possible credential or private key')
    if path.suffix == '.md' and LOCAL_PATH.search(text):
        errors.append('use generic paths rather than personal filesystem paths')
    return errors


def check_files(files):
    errors = []
    for name in sorted(REQUIRED - files.keys()):
        errors.append(name + ': required public file is missing')
    for name, data in sorted(files.items()):
        errors.extend(name + ': ' + message for message in check_content(name, data))
        if name.endswith('.md'):
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', data.decode('utf-8', errors='replace')):
                if target.startswith(('https://', 'http://', 'mailto:', '#')):
                    continue
                relative = target.split('#', 1)[0]
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), relative))
                if resolved.startswith('../') or resolved not in files:
                    errors.append(name + ': unresolved local link: ' + target)
    return errors


def tree_files(root):
    files = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED and not d.endswith('.egg-info')]
        for name in dirs + names:
            if (Path(current) / name).is_symlink():
                raise ValueError('Symlinks are not allowed in the public checkout')
        for name in names:
            path = Path(current) / name
            if path.relative_to(root).as_posix() == 'extensions/vscode/runtime-manifest.json':
                continue
            if path.relative_to(root).as_posix().startswith('extensions/vscode/core/'):
                continue
            if path.suffix in {'.pyc', '.pyo'}:
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def staged_files(root):
    actual = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=root, text=True).strip()
    if Path(actual).resolve() != root.resolve():
        raise ValueError('Run this check in its own Git repository')
    raw = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root)
    files = {}
    for item in raw.split(b'\0'):
        if not item:
            continue
        metadata, name = item.split(b'\t', 1)
        mode, oid, stage = metadata.decode('ascii').split()
        if mode not in ('100644', '100755') or stage != '0':
            raise ValueError('Index contains a link, submodule, or unresolved conflict')
        files[name.decode('utf-8')] = subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=root)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--tree', action='store_true')
    mode.add_argument('--staged', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        errors = check_files(staged_files(root) if args.staged else tree_files(root))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        errors = [str(exc)]
    for error in errors:
        print(error)
    if not errors:
        print('Public release policy passed.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
