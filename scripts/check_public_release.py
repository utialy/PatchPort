"""Check public repository content and commit messages without modifying files."""
import argparse
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import struct
import zlib

ROOT_FILES = {'.gitignore', '.gitattributes', 'README.md', 'AGENTS.md', 'CLAUDE.md',
              'CONTRIBUTING.md', 'LICENSE', 'pyproject.toml', 'MANIFEST.in', 'CHANGELOG.md', 'setup.py'}
ROOT_DIRS = {'src', 'tests', 'tools', 'scripts', 'skills', 'examples', 'docs', '.github', '.githooks', 'extensions', 'desktop', 'packaging'}
EXCLUDED = {'.git', '.venv', '__pycache__', '.bridge', '.agent-bridge', '.role-flows',
            '.bridge-requests', '.bridge-integration-backups', 'workspaces', 'build', 'dist', 'node_modules'}
REQUIRED = {'README.md', 'LICENSE', 'pyproject.toml', 'AGENTS.md', 'CONTRIBUTING.md',
            'docs/COMMIT_RULES.md', 'scripts/check_public_release.py'}
TEXT_SUFFIXES = {'.py', '.md', '.toml', '.json', '.yml', '.yaml', '.in', '.ts', '.cjs', '.ps1', '.css', '.svg', '.sh', '.txt'}
SUBJECT = re.compile(r'(feat|fix|docs|test|refactor|perf|build|ci|chore|revert)(\([a-z0-9-]+\))?: [a-z].+')
TOKENS = [re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
          re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}'),
          re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}')]
LOCAL_PATH = re.compile(r'(?:[A-Za-z]:[/\\](?:Users|!Projects)[/\\]|/(?:home|Users)/[A-Za-z0-9_.-]+/)', re.I)


def check_png(data):
    """Accept bounded screenshots without text or EXIF metadata."""
    if len(data) > 5 * 1024 * 1024 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return ['screenshot must be a PNG of at most 5 MiB']
    offset, chunks = 8, []
    try:
        while offset < len(data):
            length = struct.unpack_from('>I', data, offset)[0]
            kind = data[offset + 4:offset + 8]
            end = offset + 12 + length
            if end > len(data) or kind not in (b'IHDR', b'IDAT', b'IEND', b'sRGB', b'gAMA', b'pHYs', b'cHRM'):
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
    if (any(p in EXCLUDED or p.endswith('.egg-info') for p in path.parts)
            or name.startswith('extensions/vscode/core/') or name == 'extensions/vscode/runtime-manifest.json'
            or path.name.startswith(('.env', 'HANDOFF')) or path.name == 'NEXT_SESSION.md'
            or path.suffix in {'.pyc', '.pyo', '.pyd', '.db', '.sqlite3', '.log'}):
        errors.append('private or generated file is not allowed')
    if re.fullmatch(r'docs/images/[a-z0-9-]+\.png', name):
        return errors + check_png(data)
    if name.endswith(('/.gitignore', '/.vscodeignore', '/LICENSE')):
        pass
    elif name not in ROOT_FILES and path.parts and path.parts[0] != '.githooks' and path.suffix not in TEXT_SUFFIXES:
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


def check_message(message):
    lines = message.strip().splitlines()
    if not lines:
        return ['commit message is empty']
    errors = []
    if not message.isascii():
        errors.append('write the commit message in English ASCII text')
    if len(lines[0]) > 72 or not SUBJECT.fullmatch(lines[0]):
        errors.append('use type(scope): imperative summary, at most 72 characters')
    if len(lines) > 1 and lines[1].strip():
        errors.append('separate the subject and body with a blank line')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--tree', action='store_true')
    mode.add_argument('--staged', action='store_true')
    mode.add_argument('--commit-message', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.commit_message:
            errors = check_message(args.commit_message.read_text(encoding='utf-8'))
        else:
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
