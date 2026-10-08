"""Offline POSIX wheel installation with versioned venvs and retained runtimes."""
import argparse
import email.parser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import time
import zipfile

from .install_manager import relative_name, sha
from .storage import (FileLock, atomic_json, canonical_selected_path, checked_local_path,
                      durable_write, remove_owned_launcher, write_executable)

MARKER = '.patchport-posix.json'
RECEIPT = '.patchport-install.json'
KEEP = 'Runtime venvs, project code, queues, answers, backups and receipts are retained.'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,47}', value):
        raise ValueError('Invalid build ID')
    return value


def environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'PIP_'))}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PIP_CONFIG_FILE=os.devnull)
    return env


def run(argv, timeout=120):
    result = subprocess.run([str(p) for p in argv], env=environment(), stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Local installation subprocess failed (exit {}). No retry; inspect the installation record.'.format(result.returncode))
    return result.stdout


def python_identity(python):
    python = Path(python).resolve(strict=True)
    code = ('import sys,json,platform,venv,ensurepip; '
            'assert sys.version_info >= (3,11); '
            'print(json.dumps(dict(version=list(sys.version_info[:3]),platform=sys.platform,'
            'machine=platform.machine(),base_prefix=sys.base_prefix,pip=ensurepip.version())))')
    try:
        value = json.loads(run([python, '-I', '-B', '-c', code], timeout=20))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ValueError('Select Python 3.11+ with working venv and ensurepip. '
                         'Install prerequisites separately; the destination has not been changed.') from exc
    if value['platform'] not in ('linux', 'darwin') or value['platform'] != sys.platform:
        raise ValueError('Select Python 3.11+ with venv and ensurepip for this Linux/macOS host')
    return dict(value, path=str(python), sha256=sha(python))


def wheel_info(path, expected):
    path = checked_local_path(path)
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected) or sha(path) != expected:
        raise ValueError('Wheel SHA-256 mismatch')
    if not re.fullmatch(r'local_agent_bridge-[A-Za-z0-9.!+_-]+-py3-none-any\.whl', path.name):
        raise ValueError('Expected the pure-Python PatchPort wheel')
    with zipfile.ZipFile(path) as archive:
        seen = set()
        metadata = []
        total = 0
        for item in archive.infolist():
            name = relative_name(item.orig_filename)
            if item.filename.casefold() in seen or item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe wheel entry')
            seen.add(item.filename.casefold())
            top = name.parts[0]
            if not (top == 'agent_bridge' or top.startswith('local_agent_bridge-') and
                    (top.endswith('.dist-info') or top.endswith('.data'))):
                raise ValueError('Unexpected wheel package')
            if name.suffix == '.pth':
                raise ValueError('Startup hooks are not permitted in the runtime wheel')
            total += item.file_size
            if total > 256 * 1024 * 1024 or len(seen) > 10000:
                raise ValueError('Wheel exceeds size limits')
            if item.filename.endswith('.dist-info/METADATA'):
                metadata.append(item.filename)
        if len(metadata) != 1 or 'agent_bridge/management.py' not in seen:
            raise ValueError('Incomplete runtime wheel')
        value = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
        if value['Name'].replace('_', '-').lower() != 'local-agent-bridge' or value.get_all('Requires-Dist'):
            raise ValueError('Only the dependency-free PatchPort runtime is supported')
    return dict(path=str(path), sha256=expected, version=value['Version'])


def root_state(root):
    root = canonical_selected_path(root)
    if root == Path(root.anchor) or root == Path.home().resolve() or '\n' in str(root) or '\r' in str(root):
        raise ValueError('Select a dedicated installation folder')
    marker = checked_local_path(root / MARKER, root)
    for name in ('.install.lock', 'versions', 'bin'):
        checked_local_path(root / name, root)
    if marker.exists():
        record = json.loads(marker.read_text(encoding='utf-8'))
        if record != dict(schema=1, product='PatchPort', platform=sys.platform, root=str(root)):
            raise ValueError('Root moved or ownership/platform differs; install at a fresh location')
        return root, sha(marker)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError('Unowned install root is not empty')
    return root, None


def read_receipt(version):
    path = checked_local_path(version / RECEIPT, version)
    record = json.loads(path.read_text(encoding='utf-8'))
    if (record.get('schema') != 1 or record.get('product') != 'PatchPort'
            or record.get('directory') != str(version) or record.get('key') != version.name
            or record.get('platform') != sys.platform
            or record.get('state') not in ('INSTALLING', 'INSTALL_FAILED', 'READY', 'REMOVING', 'LAUNCHER_REMOVED')):
        raise ValueError('Invalid or moved installation record')
    version_key(record['build'])
    if not isinstance(record.get('wheel_sha256'), str) or not re.fullmatch('[a-f0-9]{64}', record['wheel_sha256']):
        raise ValueError('Invalid wheel ownership')
    if record['key'] != record['build'] + '-' + record['wheel_sha256'][:12]:
        raise ValueError('Invalid version directory')
    if record.get('launcher') != 'bin/patchport-' + record['key']:
        raise ValueError('Invalid launcher ownership')
    return record


def inventory(venv):
    values = {}
    for directory, folders, files in os.walk(venv, followlinks=False):
        for name in files + folders:
            path = Path(directory) / name
            relative = path.relative_to(venv).as_posix()
            if '__pycache__' in path.parts:
                continue
            if path.is_symlink():
                if not path.resolve().is_relative_to(venv):
                    raise ValueError('Venv contains a link outside its directory')
                values[relative] = dict(link=os.readlink(path))
            elif path.is_file():
                values[relative] = dict(sha256=sha(path))
    return values


def verify_inventory(venv, values):
    if not isinstance(values, dict) or not values:
        raise ValueError('Missing runtime inventory')
    for name, expected in values.items():
        relative_name(name)
        path = venv / name
        if 'link' in expected:
            checked_local_path(path.parent, venv) if path.parent != venv else None
            if not path.is_symlink() or os.readlink(path) != expected['link'] or not path.resolve().is_relative_to(venv):
                raise ValueError('Venv link changed: ' + name)
        elif sha(checked_local_path(path, venv)) != expected['sha256']:
            raise ValueError('Installed runtime changed: ' + name)


class Plan:
    def __init__(self, action, root, version, report, guard, wheel=None, identity=None):
        self.action, self.root, self.version = action, root, version
        self.report, self.guard, self.wheel, self.identity = report, guard, wheel, identity
        self.used = False
        self.deadline = time.monotonic() + 600
        self.token = digest(dict(action=action, root=str(root), version=str(version), guard=guard,
                                 wheel=wheel, python=identity))

    def view(self):
        return dict(self.report, plan_token=self.token)


def preview_install(wheel, expected, root, build, python=None):
    version_key(build)
    identity = python_identity(python or sys.executable)
    package = wheel_info(Path(wheel), expected)
    root, marker = root_state(root)
    if Path(package['path']).is_relative_to(root):
        raise ValueError('Distribution files must be outside the install root')
    key = build + '-' + expected[:12]
    version = checked_local_path(root / 'versions' / key, root)
    launcher = checked_local_path(root / 'bin' / ('patchport-' + key), root)
    old = None
    if version.exists():
        old = read_receipt(version)
        if old['state'] != 'READY' or old['wheel_sha256'] != expected or old['python'] != identity:
            raise ValueError('Version is incomplete, removed, or conflicts with the selected Python')
        verify_inventory(version / 'venv', old['inventory'])
        if sha(launcher) != old['launcher_sha256']:
            raise ValueError('Launcher changed; preserve it')
    elif launcher.exists():
        raise ValueError('Launcher destination is already occupied')
    guard = dict(marker=marker, receipt=sha(version / RECEIPT) if old else None,
                 launcher=sha(launcher) if old else None)
    report = dict(operation='install', outcome='UNCHANGED' if old else 'PREVIEW', build=build,
                  directory=str(version), python=identity, wheel=package, launcher=str(launcher),
                  retained=KEEP, provider_calls=0, services='Not registered')
    return Plan('install', root, version, report, guard, package, identity)


def preview_remove(root, key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,70}', key):
        raise ValueError('Invalid installed version key')
    root, marker = root_state(root)
    if marker is None:
        raise ValueError('No owned installation')
    version = checked_local_path(root / 'versions' / key, root)
    old = read_receipt(version)
    if old['state'] not in ('READY', 'REMOVING', 'LAUNCHER_REMOVED'):
        raise ValueError('Incomplete installation must be preserved for inspection')
    launcher = checked_local_path(root / old['launcher'], root)
    actual = sha(launcher) if launcher.exists() else None
    if actual is None and old['state'] == 'READY':
        raise ValueError('Owned launcher missing')
    if actual is not None and actual != old['launcher_sha256']:
        raise ValueError('Modified launcher is preserved')
    guard = dict(marker=marker, receipt=sha(version / RECEIPT), launcher=actual)
    report = dict(operation='remove_launcher', outcome='PREVIEW' if actual or old['state'] == 'REMOVING' else 'UNCHANGED',
                  directory=str(version), launcher=str(launcher), retained=KEEP, provider_calls=0)
    return Plan('remove', root, version, report, guard)


def apply(plan, token):
    if plan.used or time.monotonic() >= plan.deadline or token != plan.token:
        raise ValueError('Plan expired, consumed, or differs from preview')
    plan.used = True
    if plan.action == 'install':
        fresh = preview_install(plan.wheel['path'], plan.wheel['sha256'], plan.root,
                                plan.report['build'], plan.identity['path'])
    else:
        fresh = preview_remove(plan.root, plan.version.name)
    if fresh.token != token:
        raise ValueError('Installation inputs changed; preview again')
    root = plan.root
    if plan.guard['marker'] is None:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise ValueError('Root changed before creation')
        durable_write(root / MARKER, json.dumps(dict(schema=1, product='PatchPort', platform=sys.platform,
                                                    root=str(root))).encode())
    with FileLock(checked_local_path(root / '.install.lock', root)):
        if plan.action == 'remove':
            fresh = preview_remove(root, plan.version.name)
            if fresh.token != token:
                raise ValueError('Removal target changed')
            if plan.guard['launcher'] is None:
                record = read_receipt(plan.version)
                if record['state'] == 'REMOVING':
                    record['state'] = 'LAUNCHER_REMOVED'
                    atomic_json(plan.version / RECEIPT, record)
                    return dict(plan.report, outcome='LAUNCHER_REMOVED')
                return plan.report
            record = read_receipt(plan.version)
            record['state'] = 'REMOVING'
            atomic_json(plan.version / RECEIPT, record)
            remove_owned_launcher(Path(plan.report['launcher']), plan.guard['launcher'], root)
            record['state'] = 'LAUNCHER_REMOVED'
            atomic_json(plan.version / RECEIPT, record)
            return dict(plan.report, outcome='LAUNCHER_REMOVED')
        fresh = preview_install(plan.wheel['path'], plan.wheel['sha256'], root, plan.report['build'], plan.identity['path'])
        if fresh.guard['receipt'] != plan.guard['receipt'] or fresh.guard['launcher'] != plan.guard['launcher']:
            raise ValueError('Version changed while waiting for installation lock')
        if fresh.report['outcome'] == 'UNCHANGED':
            return plan.report
        return install_new(plan)


def install_new(plan):
    version = checked_local_path(plan.version, plan.root)
    version.mkdir(parents=True, exist_ok=False)
    record = dict(schema=1, product='PatchPort', platform=sys.platform, key=version.name,
                  build=plan.report['build'], directory=str(version), wheel_sha256=plan.wheel['sha256'],
                  python=plan.identity, state='INSTALLING', launcher='bin/patchport-' + version.name,
                  launcher_sha256=None, inventory={})
    atomic_json(version / RECEIPT, record)
    try:
        run([plan.identity['path'], '-I', '-B', '-m', 'venv', '--copies', str(version / 'venv')])
        python = checked_local_path(version / 'venv/bin/python', version)
        if sha(Path(plan.wheel['path'])) != plan.wheel['sha256']:
            raise ValueError('Wheel changed before pip installation')
        run([python, '-I', '-B', '-m', 'pip', '--isolated', 'install', '--no-index', '--no-deps',
             '--no-compile', '--no-cache-dir', '--disable-pip-version-check', plan.wheel['path']])
        code = ('import json,sys,agent_bridge; from agent_bridge import management; '
                'print(json.dumps(dict(prefix=sys.prefix, module=agent_bridge.__file__,protocol=management.PROTOCOL)))')
        result = json.loads(run([python, '-I', '-B', '-c', code], timeout=20))
        if (Path(result['prefix']).resolve() != version / 'venv' or result['protocol'] != 1
                or not Path(result['module']).resolve().is_relative_to(version / 'venv')):
            raise ValueError('Installed runtime identity differs')
        record['inventory'] = inventory(version / 'venv')
        launcher = checked_local_path(plan.root / record['launcher'], plan.root)
        launcher.parent.mkdir(exist_ok=True)
        data = ('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' -I -B -m agent_bridge "$@"\n').encode()
        write_executable(launcher, data)
        record['launcher_sha256'] = hashlib.sha256(data).hexdigest()
        record['state'] = 'READY'
        atomic_json(version / RECEIPT, record)
        return dict(plan.report, outcome='INSTALLED', core_python=str(python))
    except Exception as exc:
        record.update(state='INSTALL_FAILED', error=type(exc).__name__)
        atomic_json(version / RECEIPT, record)
        return dict(plan.report, outcome='PARTIAL', error=str(exc), record=str(version / RECEIPT),
                    next_action='Preserve this failed version; inspect it and choose a new build ID or install root.')


def main(argv=None, *, distribution=None):
    parser = argparse.ArgumentParser(description='Preview or apply an offline PatchPort POSIX installation')
    parser.add_argument('--root', type=Path, required=True, help='Dedicated installation directory')
    parser.add_argument('--apply', action='store_true', help='Apply only the reviewed plan token')
    parser.add_argument('--expect-plan', help='Exact plan_token returned by a previous preview')
    parser.add_argument('--remove-launcher', metavar='VERSION_KEY', help='Remove only an owned launcher; retain the runtime')
    parser.add_argument('--list', action='store_true', help='Read installed version records')
    args = parser.parse_args(argv)
    try:
        if os.name != 'posix' or sys.platform not in ('linux', 'darwin'):
            raise ValueError('This installer targets Linux and macOS with Python 3.11+ and venv/ensurepip')
        if not args.root.is_absolute():
            raise ValueError('Use an absolute installation root')
        if args.list:
            if args.apply or args.remove_launcher or args.expect_plan:
                raise ValueError('--list cannot mutate an installation')
            root, marker = root_state(args.root)
            parent = root / 'versions'
            result = dict(versions=[read_receipt(checked_local_path(p, root)) for p in sorted(parent.iterdir())]
                          if marker and parent.exists() else [])
            result['versions'] = [{k: v for k, v in r.items() if k != 'inventory'} for r in result['versions']]
        else:
            if args.remove_launcher:
                plan = preview_remove(args.root, args.remove_launcher)
            else:
                if distribution is None:
                    raise ValueError('Run install.sh from the verified offline distribution')
                plan = preview_install(distribution['wheel'], distribution['sha256'], args.root,
                                       distribution['build'], sys.executable)
            if args.apply and not args.expect_plan:
                raise ValueError('--apply requires the exact --expect-plan token from preview')
            result = apply(plan, args.expect_plan) if args.apply else plan.view()
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 2 if result.get('outcome') == 'PARTIAL' else 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(json.dumps(dict(ok=False, error=str(exc), next_action='Inspect the failure and preview again; no automatic retry')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
