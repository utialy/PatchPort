"""Offline, versioned desktop installation with explicit ownership and retention."""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import time
import zipfile

from . import child
from .storage import (FileLock, atomic_json, checked_local_path, durable_write,
                      guarded_removal)

PRODUCT = 'PatchPort'
RECEIPT = '.patchport-install.json'
ROOT_MARKER = '.patchport-root.json'
STATES = {'INSTALLING', 'READY', 'INSTALL_FAILED', 'REMOVING', 'REMOVE_FAILED', 'GUI_REMOVED'}
KEEP_NOTE = ('Core runtime, notices, ownership records, and all project data are retained. '
             'Projects outside the app registry may still reference this runtime.')


def sha(path):
    with checked_local_path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative_name(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('Invalid package path')
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name:
        raise ValueError('Noncanonical package path')
    for part in path.parts:
        if (part in ('.', '..') or part.endswith(('.', ' ')) or any(c in part for c in ':<>"|?*')
                or any(ord(c) < 32 for c in part)
                or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                                  *('COM' + str(n) for n in range(1, 10)),
                                                  *('LPT' + str(n) for n in range(1, 10))}):
            raise ValueError('Unsafe package path: ' + name)
    return path


def file_table(files):
    if not isinstance(files, dict) or not files or len(files) > 20000:
        raise ValueError('Invalid package file table')
    seen = set()
    total = 0
    for name, item in files.items():
        path = relative_name(name)
        if name.casefold() in seen or path.parts[0] not in ('core', 'gui', 'notices', 'package.json'):
            raise ValueError('Duplicate or unexpected package file')
        if path.parts[0] == 'package.json' and name != 'package.json':
            raise ValueError('Invalid bundle manifest path')
        seen.add(name.casefold())
        if (not isinstance(item, dict) or set(item) != {'size', 'sha256'}
                or type(item['size']) is not int or not 0 <= item['size'] <= 512 * 1024 * 1024
                or not isinstance(item['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['sha256'])):
            raise ValueError('Invalid file size or SHA-256')
        total += item['size']
    if total > 2 * 1024**3:
        raise ValueError('Package exceeds the installation size limit')
    for name in seen:
        if any(str(p).casefold() in seen for p in PurePosixPath(name).parents if str(p) != '.'):
            raise ValueError('Package file/directory collision')
    required = {'core/python.exe', 'gui/PatchPort.exe', 'package.json', 'notices/PYTHON-LICENSE.txt'}
    if not required <= files.keys():
        raise ValueError('Package is missing required runtime, GUI, or notice files')


class Package:
    def __init__(self, path):
        self.path = checked_local_path(path)
        self.digest = sha(self.path)
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo('install-manifest.json')
            if info.file_size > 4 * 1024 * 1024:
                raise ValueError('Manifest too large')
            manifest = json.loads(archive.read(info))
            if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'product', 'version', 'files'}
                    or manifest['schema'] != 1 or manifest['product'] != PRODUCT
                    or not isinstance(manifest['version'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,47}', manifest['version'])):
                raise ValueError('Invalid installation manifest')
            file_table(manifest['files'])
            expected = set(manifest['files']) | {'install-manifest.json'}
            names = []
            for item in archive.infolist():
                relative_name(item.orig_filename)
                if item.is_dir() or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                    raise ValueError('Unsupported archive entry')
                names.append(item.filename)
                if item.filename != 'install-manifest.json':
                    record = manifest['files'].get(item.filename)
                    if record is None or item.file_size != record['size']:
                        raise ValueError('Archive differs from file table')
                    with archive.open(item) as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                            raise ValueError('Payload SHA-256 mismatch')
            if len(names) != len(set(names)) or set(names) != expected:
                raise ValueError('Archive contains duplicate or unexpected entries')
            bundle = json.loads(archive.read('package.json').decode('utf-8-sig'))
            if bundle.get('core_python') != 'core/python.exe' or bundle.get('gui') != 'gui/PatchPort.exe':
                raise ValueError('Unexpected bundle entry points')
        if sha(self.path) != self.digest:
            raise ValueError('Package changed during validation')
        self.manifest = manifest
        self.version = manifest['version']
        self.key = self.version + '-' + self.digest[:12]


def root_state(root):
    root = checked_local_path(root)
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError('Select a dedicated installation folder')
    marker = checked_local_path(root / ROOT_MARKER, root)
    checked_local_path(root / '.install.lock', root)
    checked_local_path(root / 'versions', root)
    if marker.exists():
        value = json.loads(marker.read_text(encoding='utf-8'))
        if value != dict(schema=1, product=PRODUCT, root=str(root)):
            raise ValueError('Installation root ownership differs')
        return root, sha(marker)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError('Unowned installation folder is not empty')
    return root, None


def receipt(version):
    version = checked_local_path(version)
    path = checked_local_path(version / RECEIPT, version)
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('product') != PRODUCT
            or value.get('directory') != str(version) or value.get('state') not in STATES
            or value.get('key') != version.name or not isinstance(value.get('package_sha256'), str)
            or not re.fullmatch('[a-f0-9]{64}', value['package_sha256'])):
        raise ValueError('Invalid installation ownership record')
    file_table(value['files'])
    shortcut = value.get('shortcut')
    if shortcut is not None and (not isinstance(shortcut, dict) or set(shortcut) != {'name', 'sha256'}
            or shortcut['name'] != 'PatchPort-' + value['key'] + '.lnk'
            or not isinstance(shortcut['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', shortcut['sha256'])):
        raise ValueError('Invalid shortcut ownership')
    return value


def installed_versions(root):
    root, marker = root_state(root)
    if marker is None:
        return []
    parent = root / 'versions'
    if not parent.exists():
        return []
    values = []
    for version in sorted(parent.iterdir()):
        checked_local_path(version, root)
        values.append(dict(path=str(version), **receipt(version)))
    return values


class Plan:
    """Memory-only intent; never deserialize plans supplied by clients."""
    def __init__(self, action, root, snapshot, report, package=None, version=None, shortcut=False):
        self.action, self.root, self.snapshot = action, root, snapshot
        self.report = copy.deepcopy(report)
        self.package, self.version, self.shortcut = package, version, shortcut
        self.deadline = time.monotonic() + 600
        self.used = False


def preview_install(package_path, root, *, shortcut=True):
    package = Package(package_path)
    root, marker = root_state(root)
    if package.path.is_relative_to(root):
        raise ValueError('The installer payload must be outside its destination')
    target = checked_local_path(root / 'versions' / package.key, root)
    link = checked_local_path(root / ('PatchPort-' + package.key + '.lnk'), root)
    old = None
    if target.exists():
        old = receipt(target)
        if old['state'] != 'READY' or old['package_sha256'] != package.digest or old['files'] != package.manifest['files']:
            raise ValueError('Existing version is incomplete, removed, or conflicting; preserve it')
        for name, item in old['files'].items():
            if sha(checked_local_path(target / name, target)) != item['sha256']:
                raise ValueError('Installed file changed: ' + name)
        if bool(old.get('shortcut')) != shortcut:
            raise ValueError('Existing shortcut choice differs; preserve this version')
        if shortcut and sha(link) != old['shortcut']['sha256']:
            raise ValueError('Installed shortcut changed')
    elif link.exists():
        raise ValueError('Shortcut path is already owned by another file')
    report = dict(operation='install', version=package.version, directory=str(target),
                  outcome='UNCHANGED' if old else 'PREVIEW', files=len(package.manifest['files']),
                  bytes=sum(item['size'] for item in package.manifest['files'].values()),
                  shortcut=str(link) if shortcut else None, previous_versions='Preserved',
                  projects='Unchanged; reconnect selected projects separately', provider_calls=0)
    snapshot = dict(marker=marker, receipt=sha(target / RECEIPT) if old else None)
    return Plan('install', root, snapshot, report, package=package, version=target, shortcut=shortcut)


def preview_remove(root, key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,70}', key):
        raise ValueError('Invalid installed version key')
    root, marker = root_state(root)
    if marker is None:
        raise ValueError('No owned installation root')
    version = checked_local_path(root / 'versions' / key, root)
    old = receipt(version)
    if old['state'] not in ('READY', 'REMOVING', 'REMOVE_FAILED', 'GUI_REMOVED'):
        raise ValueError('Incomplete installation is retained for inspection')
    entries = {}
    missing = []
    for name, item in old['files'].items():
        if not name.startswith('gui/'):
            continue
        path = checked_local_path(version / name, root)
        if path.exists():
            if sha(path) != item['sha256']:
                raise ValueError('Modified GUI file is preserved: ' + name)
            entries[str(path)] = item['sha256']
        elif old['state'] == 'READY':
            raise ValueError('Owned GUI file is missing: ' + name)
        else:
            missing.append(str(path))
    shortcut = old.get('shortcut')
    if shortcut:
        path = checked_local_path(root / shortcut['name'], root)
        if path.exists():
            if sha(path) != shortcut['sha256']:
                raise ValueError('Modified shortcut is preserved')
            entries[str(path)] = shortcut['sha256']
        elif old['state'] == 'READY':
            raise ValueError('Owned shortcut is missing')
    report = dict(operation='remove_gui', version=old['version'], directory=str(version),
                  outcome='UNCHANGED' if not entries else 'PREVIEW', delete=list(entries),
                  already_absent=missing, retained=KEEP_NOTE, provider_calls=0)
    return Plan('remove', root, dict(marker=marker, receipt=sha(version / RECEIPT), entries=entries),
                report, version=version)


def apply(plan, *, verify=None, make_shortcut=None):
    if not isinstance(plan, Plan) or plan.used or time.monotonic() >= plan.deadline:
        raise ValueError('Installation plan expired or already consumed; preview again')
    plan.used = True
    root, marker = root_state(plan.root)
    if marker != plan.snapshot['marker']:
        raise ValueError('Installation root changed since preview')
    if plan.action == 'install':
        fresh = preview_install(plan.package.path, root, shortcut=plan.shortcut)
        if fresh.package.digest != plan.package.digest or fresh.snapshot != plan.snapshot:
            raise ValueError('Package or destination changed since preview')
    elif preview_remove(root, plan.version.name).snapshot != plan.snapshot:
        raise ValueError('Owned installation changed since preview')
    if marker is None:
        # Only an exclusive new directory may be claimed; empty existing roots are
        # allowed after a second emptiness check, under the root creation boundary.
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise ValueError('Installation destination changed')
        durable_write(root / ROOT_MARKER, json.dumps(dict(schema=1, product=PRODUCT, root=str(root))).encode())
    with FileLock(checked_local_path(root / '.install.lock', root)):
        if plan.action == 'install':
            return _install(plan, verify or child.verify_installed_core,
                            make_shortcut or child.create_windows_shortcut)
        return _remove(plan)


def _install(plan, verify, make_shortcut):
    fresh = preview_install(plan.package.path, plan.root, shortcut=plan.shortcut)
    if (fresh.package.digest != plan.package.digest
            or fresh.snapshot['receipt'] != plan.snapshot['receipt']):
        raise ValueError('Package or installed version changed since preview')
    if fresh.report['outcome'] == 'UNCHANGED':
        return fresh.report
    target = checked_local_path(plan.version, plan.root)
    target.mkdir(parents=True, exist_ok=False)
    record = dict(schema=1, product=PRODUCT, version=plan.package.version, key=plan.package.key,
                  directory=str(target), package_sha256=plan.package.digest,
                  files=plan.package.manifest['files'], state='INSTALLING', shortcut=None)
    atomic_json(target / RECEIPT, record)
    try:
        with zipfile.ZipFile(plan.package.path) as archive:
            for name, item in record['files'].items():
                path = checked_local_path(target / name, target)
                path.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(name)
                if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise ValueError('Payload changed during extraction')
                durable_write(path, data)
        for name, item in record['files'].items():
            if sha(target / name) != item['sha256']:
                raise ValueError('Installed content changed before verification')
        verify(target / 'core/python.exe')
        if plan.shortcut:
            link = checked_local_path(plan.root / ('PatchPort-' + plan.package.key + '.lnk'), plan.root)
            if link.exists():
                raise ValueError('Shortcut destination changed')
            make_shortcut(link, target / 'gui/PatchPort.exe')
            record['shortcut'] = dict(name=link.name, sha256=sha(link))
        record['state'] = 'READY'
        atomic_json(target / RECEIPT, record)
        return dict(plan.report, outcome='INSTALLED', gui=str(target / 'gui/PatchPort.exe'))
    except Exception as exc:
        record.update(state='INSTALL_FAILED', error=type(exc).__name__)
        atomic_json(target / RECEIPT, record)
        return dict(plan.report, outcome='PARTIAL', error=str(exc), record=str(target / RECEIPT),
                    next_action='Preserve this directory. Inspect the failure; do not reuse it automatically.')


def _remove(plan):
    fresh = preview_remove(plan.root, plan.version.name)
    if fresh.snapshot != plan.snapshot:
        raise ValueError('Owned installation changed since preview')
    if not plan.snapshot['entries']:
        return dict(plan.report, outcome='UNCHANGED')
    record = receipt(plan.version)
    # All candidates must open successfully before the first deletion or journal change.
    with guarded_removal(plan.root, [(Path(p), h) for p, h in plan.snapshot['entries'].items()]) as remove:
        record.update(state='REMOVING', removed=[])
        atomic_json(plan.version / RECEIPT, record)
        try:
            for path in remove():
                record['removed'].append(str(path))
            record['state'] = 'GUI_REMOVED'
            atomic_json(plan.version / RECEIPT, record)
            return dict(plan.report, outcome='GUI_REMOVED', removed=record['removed'])
        except Exception as exc:
            record.update(state='REMOVE_FAILED', error=type(exc).__name__)
            atomic_json(plan.version / RECEIPT, record)
            return dict(plan.report, outcome='PARTIAL', removed=record['removed'], error=str(exc),
                        next_action='Inspect retained files, then explicitly preview again to continue.')
