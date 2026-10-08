"""Experimental user LaunchAgent adapter; native macOS validation is separate."""
import json
import os
from pathlib import Path
import plistlib
import re
import sys
import time
import uuid

from . import child
from .storage import (FileLock, atomic_json, canonical_selected_path, checked_local_path,
                      check_private_owner, private_write, remove_owned_launcher)
from .service_manager import RECORD, digest, fingerprint

BACKEND = 'launchd-user'
STATES = ('REGISTERING', 'READY', 'CHANGING', 'PARTIAL', 'REMOVING')
INACTIVE = ('not running', 'exited', 'waiting')


def directories():
    home = Path.home()
    return (canonical_selected_path(home / 'Library/Application Support/PatchPort/Agents'),
            canonical_selected_path(home / 'Library/LaunchAgents'))


def label(config, uid):
    return 'com.patchport.' + fingerprint(dict(config=str(config['config_path']), state=str(config['state']), uid=uid))[:24]


def arguments(identity, record_id):
    values = [identity['python'], '-I', '-B', '-m', 'agent_bridge', '--config', identity['config'],
              'service', 'run', '--registration-id', record_id]
    for value in [*values, identity['root']]:
        if not isinstance(value, str) or any(ord(c) < 32 for c in value) or value != value.strip():
            raise ValueError('Control characters and outer whitespace are not supported in LaunchAgent paths')
    return values


def render(identity, record_id, name, *, automatic=False):
    return plistlib.dumps(dict(Label=name, ProgramArguments=arguments(identity, record_id),
        WorkingDirectory=identity['root'], RunAtLoad=automatic, KeepAlive=False, ExitTimeOut=30,
        EnvironmentVariables={'PYTHONUTF8': '1', 'PATH': '/usr/local/bin:/usr/bin:/bin'}, Umask=0o077),
        fmt=plistlib.FMT_XML, sort_keys=True)


def validate_record(config, record):
    if (record.get('protocol') != 1 or type(record.get('uid')) is not int or record.get('backend') != BACKEND
            or record.get('state') not in STATES or not isinstance(record.get('identity'), dict)
            or type(record.get('login_owned')) is not bool or record.get('name') != label(config, record['uid'])):
        raise ValueError('Invalid LaunchAgent ownership record')
    for name in ('id', 'unit_sha256', 'login_sha256'):
        if not isinstance(record.get(name), str) or not re.fullmatch('[a-f0-9]{64}', record[name]):
            raise ValueError('Invalid LaunchAgent identity/hash')
    identity = record['identity']
    if any(not isinstance(identity.get(k), str) or not Path(identity[k]).is_absolute()
           for k in ('python', 'config', 'root', 'state')):
        raise ValueError('Invalid LaunchAgent runtime paths')
    if any(identity.get(k) != str(v) for k,v in [('config', config['config_path']), ('root', config['root']), ('state', config['state'])]):
        raise ValueError('LaunchAgent project identity differs')
    for name in ('unit', 'login_unit'):
        if not isinstance(record.get(name), str) or not Path(record[name]).is_absolute() or Path(record[name]).name != record['name'] + '.plist':
            raise ValueError('Invalid LaunchAgent definition path')
    if (digest(render(identity, record['id'], record['name'])) != record['unit_sha256']
            or digest(render(identity, record['id'], record['name'], automatic=True)) != record['login_sha256']):
        raise ValueError('LaunchAgent definition differs from ownership')
    pending = record.get('pending_start')
    if pending is not None and (not isinstance(pending, dict) or set(pending) != {'id', 'previous_run_id'}
            or not re.fullmatch('[a-f0-9]{32}', str(pending['id']))
            or pending['previous_run_id'] is not None and not isinstance(pending['previous_run_id'], str)):
        raise ValueError('Invalid pending LaunchAgent start')
    return record


def registration(config):
    path = checked_local_path(config['state'] / RECORD)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid service registration')
    return validate_record(config, value)


def checked_files(record, *, partial=False):
    if sys.platform != 'darwin' or record['uid'] != os.getuid() or os.getuid() == 0:
        raise ValueError('Use the owning non-root macOS user')
    manual_dir, login_dir = directories()
    manual = checked_local_path(Path(record['unit']))
    login = checked_local_path(Path(record['login_unit']))
    if manual.parent != manual_dir or login.parent != login_dir:
        raise ValueError('LaunchAgent directories differ from the owning user')
    if manual.exists():
        check_private_owner(manual)
        if digest(manual.read_bytes()) != record['unit_sha256']:
            raise ValueError('Manual LaunchAgent plist changed; preserve it')
    elif not partial:
        raise ValueError('Owned LaunchAgent definition is missing')
    if login.exists():
        check_private_owner(login)
        if not record['login_owned'] or digest(login.read_bytes()) != record['login_sha256']:
            raise ValueError('Login plist is unowned or changed; preserve it')
    elif record['login_owned'] and not (partial and record.get('operation') == 'disable'):
        raise ValueError('Owned login plist is missing')
    return manual, login


def parse_print(text, target):
    """Read only known top-level fields, never return environment dictionaries."""
    lines = text.strip().splitlines()
    if not lines or lines[0].strip() != target + ' = {' or lines[-1].strip() != '}':
        raise ValueError('Unrecognized launchctl print structure; native inspection required')
    depth, args, fields, collecting = 1, None, {}, False
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line == '}':
            if collecting and depth == 2:
                collecting = False
            depth -= 1
            if depth < 0:
                raise ValueError('Unbalanced launchctl output')
            continue
        if depth == 0:
            raise ValueError('Unexpected text after launchctl job')
        if collecting:
            args.append(line)
            continue
        if line.endswith(' = {'):
            if depth == 1 and line == 'arguments = {':
                if args is not None:
                    raise ValueError('Duplicate launchctl arguments')
                args, collecting = [], True
            depth += 1
            continue
        if depth == 1 and ' = ' in line:
            key, value = line.split(' = ', 1)
            if key in ('path', 'program', 'state', 'pid'):
                if key in fields:
                    raise ValueError('Duplicate launchctl field')
                fields[key] = value
    if depth or collecting or args is None or not {'path', 'program', 'state'} <= fields.keys():
        raise ValueError('Incomplete launchctl job description')
    pid = fields.get('pid')
    if pid is not None and not re.fullmatch('[1-9][0-9]*', pid):
        raise ValueError('Invalid launchctl PID')
    if fields['state'] == 'running' and pid is None:
        raise ValueError('Running job has no PID')
    return dict(loaded=True, path=fields['path'], program=fields['program'], state=fields['state'],
                pid=int(pid) if pid else None, arguments=args)


def parse_disabled(text, name):
    lines = text.strip().splitlines()
    if not lines or lines[0].strip() != 'disabled services = {' or lines[-1].strip() != '}':
        raise ValueError('Unrecognized launchctl disabled-state output')
    found = []
    for line in lines[1:-1]:
        if not line.strip():
            continue
        match = re.fullmatch(r'\s*"([^"\r\n]+)"\s*=>\s*(true|false|enabled|disabled)\s*', line)
        if not match:
            raise ValueError('Unrecognized launchctl disabled-state entry')
        if match[1] == name:
            found.append(match[2] in ('true', 'disabled'))
    if len(found) > 1:
        raise ValueError('Duplicate launchctl disabled entry')
    return found[0] if found else False


def manager_state(record):
    domain = 'gui/' + str(record['uid'])
    target = domain + '/' + record['name']
    # An absent GUI domain or permission failure is not an absent job.
    child.launchctl_user(['print', domain], discard_output=True)
    _, disabled, _ = child.launchctl_user(['print-disabled', domain])
    external_disabled = parse_disabled(disabled, record['name'])
    code, output, error = child.launchctl_user(['print', target], check=False)
    if code:
        if code not in (3, 113) or 'Could not find service' not in error or '"' + record['name'] + '"' not in error:
            raise RuntimeError('LaunchAgent state is unknown; inspect without automatic fallback')
        return dict(loaded=False, path=None, program=None, state='absent', pid=None, arguments=[], disabled=external_disabled)
    return dict(parse_print(output, target), disabled=external_disabled)


def owned_state(record, *, partial=False):
    checked_files(record, partial=partial)
    state = manager_state(record)
    if state['loaded'] and (state['path'] not in (record['unit'], record['login_unit'])
            or state['program'] != record['identity']['python']
            or state['arguments'] != arguments(record['identity'], record['id'])):
        raise ValueError('Loaded LaunchAgent differs from owned definition')
    return state


def proposed(config):
    if sys.platform != 'darwin' or os.getuid() == 0:
        raise ValueError('LaunchAgent registration requires a non-root macOS GUI user')
    from .runner_manager import execution_identity
    identity = execution_identity(config)
    for endpoint in config['endpoints'].values():
        if not Path(endpoint['command'][0]).is_absolute():
            raise ValueError('Select absolute provider executable paths')
    name = label(config, os.getuid())
    manual_dir, login_dir = directories()
    manual = checked_local_path(manual_dir / (name + '.plist'))
    login = checked_local_path(login_dir / (name + '.plist'))
    record_id = fingerprint(dict(identity=identity, uid=os.getuid(), unit=str(manual), login_unit=str(login), backend=BACKEND))
    return dict(protocol=1, backend=BACKEND, uid=os.getuid(), identity=identity, id=record_id, name=name,
                unit=str(manual), unit_sha256=digest(render(identity, record_id, name)), login_unit=str(login),
                login_sha256=digest(render(identity, record_id, name, automatic=True)), login_owned=False, state='READY')


def status(config):
    record = registration(config)
    if not record:
        return dict(backend='manual', registered=False, validation='macOS native validation pending')
    state = owned_state(record, partial=record['state'] != 'READY')
    return dict(backend=BACKEND, registered=True, registration_state=record['state'], name=record['name'],
                unit=record['unit'], login_unit=record['login_unit'], manager=state,
                automatic_start=record['login_owned'], externally_disabled=state['disabled'],
                pending_start=record.get('pending_start'), validation='EXPERIMENTAL: macOS native validation pending')


def guard_execution(config, backend, service_id, *, running=False):
    record = registration(config)
    if backend != BACKEND or not record or record['state'] != 'READY' or record['id'] != service_id:
        raise ValueError('LaunchAgent does not authorize this runner')
    from .runner_manager import execution_identity
    if record['identity'] != execution_identity(config):
        raise ValueError('LaunchAgent runtime/config changed; stop, disable, unregister and reconnect')
    checked_files(record)
    if running:
        state = owned_state(record)
        if not state['loaded'] or state['pid'] != os.getpid() or os.getppid() != 1:
            raise ValueError('Internal service entry must be started by its owning launchd job')


def preview(config, action):
    if action not in ('register', 'enable', 'disable', 'unregister'):
        raise ValueError('Invalid LaunchAgent action')
    from .runner_manager import inspect
    current = registration(config)
    record = proposed(config) if action == 'register' else current
    if record is None:
        raise ValueError('Register the LaunchAgent first')
    if current:
        state = owned_state(current, partial=current['state'] != 'READY')
        if action == 'register' and any(current.get(k) != v for k,v in record.items() if k not in ('login_owned',)):
            raise ValueError('Existing registration differs; inspect before replacing')
    else:
        state = manager_state(record)
        if state['loaded'] or state['disabled'] or any(Path(record[k]).exists() or Path(record[k]).is_symlink() for k in ('unit', 'login_unit')):
            raise ValueError('Pre-existing LaunchAgent or override is not adopted')
    observed = inspect(config)
    if action in ('register', 'unregister'):
        if observed['lock_held'] or state['pid'] or state['state'] not in (*INACTIVE, 'absent'):
            raise ValueError('Stop the runner and LaunchAgent before changing backend')
        if any(p in observed['problems'] for p in ('LAUNCH_UNCONFIRMED', 'LAUNCH_UNKNOWN', 'DATABASE_UNKNOWN')):
            raise ValueError('Runner launch/database is unknown')
    if action == 'unregister' and current['login_owned']:
        raise ValueError('Disable automatic start before unregistering')
    if action == 'enable' and (current['state'] != 'READY' or state['disabled'] or current.get('pending_start')):
        raise ValueError('Partial, pending or externally disabled LaunchAgent needs inspection')
    guard = dict(record=digest((config['state'] / RECORD).read_bytes()) if current else None,
        manual=digest(Path(record['unit']).read_bytes()) if Path(record['unit']).exists() else None,
        login=digest(Path(record['login_unit']).read_bytes()) if Path(record['login_unit']).exists() else None,
        manager=state, identity=record['identity'])
    return dict(action=action, backend=BACKEND, name=record['name'], unit=record['unit'], login_unit=record['login_unit'],
        definition=render(record['identity'], record['id'], record['name']).decode(), record=record,
        guard=guard, plan_token=fingerprint(dict(action=action, guard=guard)), starts_now=False,
        pending_work_may_run=action == 'enable', validation='EXPERIMENTAL: macOS native validation pending')


def apply(config, action, token):
    plan = preview(config, action)
    if plan['plan_token'] != token:
        raise ValueError('LaunchAgent plan changed; preview again')
    with FileLock(config['state'] / 'runner-management.lock'):
        fresh = preview(config, action)
        if fresh['plan_token'] != token:
            raise ValueError('LaunchAgent changed while acquiring the lock')
        record = registration(config)
        if action in ('enable', 'disable'):
            login = Path(record['login_unit'])
            desired = action == 'enable'
            if record['state'] == 'READY' and record['login_owned'] == desired:
                return dict(status(config), outcome='UNCHANGED')
            record.update(state='CHANGING', operation=action)
            atomic_json(config['state'] / RECORD, record)
            try:
                if desired:
                    login.parent.mkdir(parents=True, exist_ok=True)
                    private_write(checked_local_path(login), render(record['identity'], record['id'], record['name'], automatic=True))
                    record['login_owned'] = True
                else:
                    if login.exists():
                        remove_owned_launcher(login, record['login_sha256'], login.parent)
                    record['login_owned'] = False
                record.update(state='READY')
                record.pop('operation', None)
                atomic_json(config['state'] / RECORD, record)
                return dict(status(config), outcome=action.upper(), starts_now=False)
            except Exception as exc:
                record.update(state='PARTIAL', error=type(exc).__name__)
                atomic_json(config['state'] / RECORD, record)
                return dict(outcome='PARTIAL', ok=False, next_action='Inspect both plist paths and service.json before explicit cleanup')
        with FileLock(config['state'] / 'runner.lock'):
            if action == 'register':
                if record:
                    return dict(status(config), outcome='UNCHANGED')
                record = dict(fresh['record'], state='REGISTERING')
                child.verify_service_runtime(Path(record['identity']['python']), record['identity']['code'])
                atomic_json(config['state'] / RECORD, record)
                try:
                    path = Path(record['unit'])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    private_write(checked_local_path(path), render(record['identity'], record['id'], record['name']))
                    record['state'] = 'READY'
                    atomic_json(config['state'] / RECORD, record)
                    return dict(status(config), outcome='REGISTERED')
                except Exception as exc:
                    record.update(state='PARTIAL', error=type(exc).__name__)
                    atomic_json(config['state'] / RECORD, record)
                    return dict(outcome='PARTIAL', ok=False, next_action='Inspect the partial registration; explicitly unregister')
            record.update(state='REMOVING')
            atomic_json(config['state'] / RECORD, record)
            state = owned_state(record, partial=True)
            if state['loaded']:
                child.launchctl_user(['bootout', 'gui/' + str(record['uid']) + '/' + record['name']])
            if manager_state(record)['loaded']:
                raise RuntimeError('Bootout not confirmed; preserve registration')
            path = Path(record['unit'])
            if path.exists():
                remove_owned_launcher(path, record['unit_sha256'], path.parent)
            marker = checked_local_path(config['state'] / RECORD)
            remove_owned_launcher(marker, digest(marker.read_bytes()), config['state'])
            return dict(outcome='UNREGISTERED', backend='manual', retained='Runtime, project data, queue, answers and usage')


def start(config, timeout=10):
    from . import runner_manager
    runner_manager.validate_timeout(timeout)
    with FileLock(config['state'] / 'runner-management.lock'):
        record = registration(config)
        if not record:
            raise ValueError('LaunchAgent registration missing')
        guard_execution(config, BACKEND, record['id'])
        state = owned_state(record)
        observed = runner_manager.inspect(config)
        lifecycle = observed['management'] or {}
        health = observed.get('health')
        if state['disabled']:
            raise ValueError('An external launchctl disabled override must be inspected by its owner')
        if observed['lock_held']:
            if (lifecycle.get('backend') == BACKEND and lifecycle.get('service_id') == record['id']
                    and lifecycle.get('mode') == 'RUNNING' and observed['heartbeat_status'] == 'FRESH'
                    and isinstance(health, dict) and isinstance(health.get('management'), dict) and state['pid'] == health.get('pid')
                    and (health.get('management') or {}).get('run_id') == lifecycle.get('run_id')):
                if record.pop('pending_start', None):
                    atomic_json(config['state'] / RECORD, record)
                return runner_manager.response('REUSED', lifecycle['run_id'], backend=BACKEND), 0
            raise ValueError('Existing runner cannot be reused')
        if record.get('pending_start'):
            raise ValueError('Previous LaunchAgent start is unconfirmed; inspect or explicitly unregister after stopping')
        if state['pid'] or state['state'] not in (*INACTIVE, 'absent'):
            raise ValueError('LaunchAgent is transitioning; inspect instead of retrying')
        if any(p in observed['problems'] for p in ('LAUNCH_UNCONFIRMED', 'LAUNCH_UNKNOWN', 'DATABASE_UNKNOWN')):
            raise ValueError('Runner state is unknown')
        domain = 'gui/' + str(record['uid'])
        if not state['loaded']:
            child.launchctl_user(['bootstrap', domain, record['unit']])
            state = owned_state(record)
            if not state['loaded'] or state['pid'] or state['state'] not in INACTIVE:
                raise RuntimeError('Manual bootstrap outcome is unexpected; inspect before starting')
        record['pending_start'] = dict(id=uuid.uuid4().hex, previous_run_id=lifecycle.get('run_id'))
        atomic_json(config['state'] / RECORD, record)
        child.launchctl_user(['kickstart', domain + '/' + record['name']])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observed = runner_manager.inspect(config)
            lifecycle = observed['management'] or {}
            if (observed['lock_held'] and lifecycle.get('backend') == BACKEND and lifecycle.get('service_id') == record['id']
                    and lifecycle.get('mode') == 'RUNNING' and lifecycle.get('run_id') != record['pending_start']['previous_run_id']):
                state = owned_state(record)
                health = observed.get('health')
                if (isinstance(health, dict) and isinstance(health.get('management'), dict) and state['pid'] == health.get('pid')
                        and (health.get('management') or {}).get('run_id') == lifecycle.get('run_id')):
                    record.pop('pending_start')
                    atomic_json(config['state'] / RECORD, record)
                    return runner_manager.response('STARTED', lifecycle['run_id'], True, backend=BACKEND), 0
            time.sleep(.05)
        return runner_manager.response('START_UNCONFIRMED', accepted=True, backend=BACKEND), 2


def stop(config, run_id, cancel=False, timeout=10):
    from . import runner_manager
    with FileLock(config['state'] / 'runner-management.lock'):
        record = registration(config)
        if not record:
            raise ValueError('LaunchAgent registration missing')
        initial = owned_state(record, partial=record['state'] != 'READY')
        lifecycle = runner_manager.inspect(config)['management'] or {}
        if lifecycle.get('backend') != BACKEND or lifecycle.get('service_id') != record['id'] or lifecycle.get('run_id') != run_id:
            raise ValueError('Observed run does not belong to this LaunchAgent')
        result, code = runner_manager.stop(config, run_id, cancel, timeout, _service=True, _lock_held=True)
        if code == 0:
            current = runner_manager.inspect(config)
            if current['lock_held'] or (current['management'] or {}).get('run_id') != run_id:
                raise ValueError('Runner changed after stop acknowledgement')
            state = owned_state(record, partial=record['state'] != 'READY')
            if state['pid'] is not None and state['pid'] != initial['pid']:
                raise ValueError('LaunchAgent PID changed after stop acknowledgement')
            if state['loaded']:
                child.launchctl_user(['bootout', 'gui/' + str(record['uid']) + '/' + record['name']])
            if manager_state(record)['loaded']:
                raise RuntimeError('LaunchAgent removal is unconfirmed; inspect')
            if record.pop('pending_start', None):
                atomic_json(config['state'] / RECORD, record)
        return dict(result, backend=BACKEND), code
