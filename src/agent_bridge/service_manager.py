"""Explicit user-service ownership and backend dispatch; no automatic retries."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from . import child
from .storage import FileLock, atomic_json, canonical_selected_path, checked_local_path, durable_write, remove_owned_launcher

RECORD = 'service.json'
BACKEND = 'systemd-user'
LAUNCHD = 'launchd-user'
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'FragmentPath',
              'DropInPaths', 'NeedDaemonReload', 'MainPID')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def unit_name(config, uid):
    return 'patchport-' + fingerprint(dict(config=str(config['config_path']), state=str(config['state']), uid=uid))[:24] + '.service'


def unit_directory():
    base = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    if not base.is_absolute():
        raise ValueError('XDG_CONFIG_HOME must be absolute')
    return canonical_selected_path(base / 'systemd/user')


def quote(value, *, command=False):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError('Control characters are not supported in service paths')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def render(identity, registration_id):
    # systemd forbids dollar signs in the executable token even when escaped.
    # env execs the explicit Python argument without a shell or a second daemon.
    argv = ['/usr/bin/env', '--chdir=' + identity['root'], '--', identity['python'], '-I', '-B', '-m', 'agent_bridge', '--config', identity['config'],
            'service', 'run', '--registration-id', registration_id]
    return ('[Unit]\nDescription=PatchPort project runner\n\n[Service]\nType=exec\n'
            + 'ExecStart=' + ' '.join(quote(p, command=True) for p in argv) + '\n'
            + 'Environment="PYTHONUTF8=1" "PATH=/usr/local/bin:/usr/bin:/bin"\n'
            + 'Restart=no\nKillMode=mixed\nTimeoutStopSec=30\nUMask=0077\n'
            + '\n[Install]\nWantedBy=default.target\n').encode()


def registration(config):
    path = checked_local_path(config['state'] / RECORD)
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(record, dict) and record.get('backend') == LAUNCHD:
        from .launchd_service import validate_record
        return validate_record(config, record)
    if (not isinstance(record, dict) or record.get('protocol') != 1 or record.get('backend') != BACKEND
            or type(record.get('uid')) is not int or not isinstance(record.get('identity'), dict)
            or record.get('state') not in ('REGISTERING', 'READY', 'PARTIAL', 'REMOVING')
            or not isinstance(record.get('id'), str) or not re.fullmatch('[a-f0-9]{64}', record['id'])
            or not isinstance(record.get('unit_sha256'), str) or not re.fullmatch('[a-f0-9]{64}', record['unit_sha256'])
            or record.get('name') != unit_name(config, record['uid'])):
        raise ValueError('Invalid service ownership; preserve it and inspect manually')
    identity = record['identity']
    if any(identity.get(k) != str(v) for k, v in [('config', config['config_path']), ('root', config['root']), ('state', config['state'])]):
        raise ValueError('Service target identity differs')
    unit = Path(record['unit'])
    if not unit.is_absolute() or unit.name != record['name']:
        raise ValueError('Invalid service unit path')
    if digest(render(identity, record['id'])) != record['unit_sha256']:
        raise ValueError('Service definition does not match its ownership record')
    return record


def guard_execution(config, backend, service_id, *, running=False):
    record = registration(config)
    if backend == 'manual':
        if record:
            raise ValueError('This project is registered as a service; use runner start/stop or unregister first')
        return
    if backend == LAUNCHD:
        from .launchd_service import guard_execution as guard_launchd
        return guard_launchd(config, backend, service_id, running=running)
    if (backend != BACKEND or not record or record['state'] != 'READY' or record['id'] != service_id
            or sys.platform != 'linux' or record['uid'] != os.getuid()):
        raise ValueError('Service registration does not authorize this runner')
    from .runner_manager import execution_identity
    if record['identity'] != execution_identity(config):
        raise ValueError('Service runtime/config changed; unregister and preview registration again')
    checked_unit(record)
    if running:
        state = owned_state(record)
        if (not re.fullmatch('[a-f0-9]{32}', os.environ.get('INVOCATION_ID', ''))
                or state['MainPID'] != str(os.getpid())):
            raise ValueError('The internal service entry must be launched by its owning systemd unit')


def manager_state(name):
    code, output = child.systemd_user(['show', name, '--property=' + ','.join(PROPERTIES)], check=False)
    values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if code and values.get('LoadState') != 'not-found' or not values.get('LoadState'):
        raise RuntimeError('The systemd user manager is unavailable; inspect without automatic fallback')
    return {key: values.get(key, '') for key in PROPERTIES}


def checked_unit(record, *, allow_missing=False):
    if sys.platform != 'linux' or record['uid'] != os.getuid():
        raise ValueError('Use the owning Linux user to manage this service')
    path = checked_local_path(Path(record['unit']))
    if path.parent != unit_directory():
        raise ValueError('Service directory differs from the registering user environment')
    checked_local_path(path.with_name(path.name + '.d'))
    if path.exists():
        if digest(path.read_bytes()) != record['unit_sha256']:
            raise ValueError('Service file was edited; preserve it')
    elif not allow_missing:
        raise ValueError('Owned service unit is missing; inspect before changing backend')
    if path.with_name(path.name + '.d').exists():
        raise ValueError('Service drop-ins need manual inspection')
    return path


def owned_state(record, *, partial=False):
    path = checked_unit(record, allow_missing=partial)
    state = manager_state(record['name'])
    if state['LoadState'] == 'not-found' and partial:
        return state
    if partial and state['LoadState'] == 'bad-setting' and state['FragmentPath'] == str(path) and not state['DropInPaths']:
        return state
    if (state['LoadState'] != 'loaded' or state['FragmentPath'] != str(path)
            or state['DropInPaths'] or (not partial and state['NeedDaemonReload'] != 'no')):
        raise ValueError('Loaded service definition differs or needs inspection')
    return state


def status(config):
    adapter = launchd_backend(config)
    if adapter:
        return adapter.status(config)
    record = registration(config)
    if record is None:
        return dict(backend='manual', registered=False, next_action='Preview service register if a user manager is available')
    state = owned_state(record, partial=record['state'] != 'READY')
    return dict(backend=BACKEND, registered=True, registration_state=record['state'], name=record['name'],
                unit=record['unit'], manager=state, automatic_start=state['UnitFileState'].startswith('enabled'),
                next_action='Use explicit start/stop/enable/disable; queued work may run when started')


def proposed(config):
    if sys.platform != 'linux' or os.getuid() == 0:
        raise ValueError('Only a non-root Linux systemd user session is implemented; use manual runner mode elsewhere')
    from .runner_manager import execution_identity
    identity = execution_identity(config)
    for endpoint in config['endpoints'].values():
        if not Path(endpoint['command'][0]).is_absolute():
            raise ValueError('Select absolute provider executables before service registration')
    name = unit_name(config, os.getuid())
    unit = checked_local_path(unit_directory() / name)
    record_id = fingerprint(dict(identity=identity, uid=os.getuid(), unit=str(unit), backend=BACKEND))
    return dict(protocol=1, backend=BACKEND, uid=os.getuid(), identity=identity, id=record_id,
                name=name, unit=str(unit), unit_sha256=digest(render(identity, record_id)), state='READY')


def preview(config, action):
    adapter = launchd_backend(config)
    if adapter:
        return adapter.preview(config, action)
    if action not in ('register', 'enable', 'disable', 'unregister'):
        raise ValueError('Invalid service plan action')
    from .runner_manager import inspect
    current = registration(config)
    expected = proposed(config) if action == 'register' else current
    if expected is None:
        raise ValueError('Register this project first')
    state = owned_state(current, partial=current['state'] != 'READY') if current else manager_state(expected['name'])
    path = checked_local_path(Path(expected['unit']))
    activation = path.parent / 'default.target.wants' / expected['name']
    if current is None and (path.exists() or state['LoadState'] != 'not-found'
                            or state['UnitFileState'].startswith('enabled')
                            or activation.exists() or activation.is_symlink()):
        raise ValueError('An unowned service already uses this unit name or path')
    if current and action == 'register' and current != expected:
        raise ValueError('Service differs; stop, disable and unregister it before registering again')
    observed = inspect(config)
    if action in ('register', 'unregister'):
        if observed['lock_held'] or state['ActiveState'] not in ('inactive', 'failed'):
            raise ValueError('Stop the runner and service before changing backend')
        if any(p in observed['problems'] for p in ('LAUNCH_UNCONFIRMED', 'LAUNCH_UNKNOWN', 'DATABASE_UNKNOWN')):
            raise ValueError('Runner launch/database is unknown; inspect before changing backend')
    if action == 'unregister' and state['UnitFileState'].startswith('enabled'):
        raise ValueError('Disable automatic start before unregistering')
    if action in ('enable', 'disable') and current['state'] != 'READY':
        raise ValueError('Partial registration must be inspected and explicitly unregistered')
    guard = dict(record=digest((config['state'] / RECORD).read_bytes()) if current else None,
                 unit=digest(path.read_bytes()) if path.exists() else None, manager=state,
                 identity=expected['identity'])
    return dict(action=action, backend=BACKEND, name=expected['name'], unit=str(path),
                definition=render(expected['identity'], expected['id']).decode(),
                record=expected, guard=guard, plan_token=fingerprint(dict(action=action, guard=guard)),
                pending_work_may_run=action == 'enable', starts_now=False,
                next_action='Apply this exact plan token. Register/enable/disable do not start the service.')


def apply(config, action, token):
    adapter = launchd_backend(config)
    if adapter:
        return adapter.apply(config, action, token)
    plan = preview(config, action)
    if token != plan['plan_token']:
        raise ValueError('Service plan changed; preview again')
    lock = checked_local_path(config['state'] / 'runner-management.lock')
    with FileLock(lock):
        fresh = preview(config, action)
        if fresh['plan_token'] != token:
            raise ValueError('Service changed while acquiring the management lock')
        if action in ('enable', 'disable'):
            child.systemd_user([action, fresh['name']])
            result = status(config)
            if result['automatic_start'] != (action == 'enable'):
                raise RuntimeError('Automatic start outcome is unconfirmed; inspect status')
            return dict(result, outcome=action.upper(), starts_now=False)
        # runner.lock closes the last gap with a foreground/manual service launch.
        with FileLock(config['state'] / 'runner.lock'):
            if action == 'register':
                if registration(config):
                    return dict(outcome='UNCHANGED', **status(config))
                child.verify_service_runtime(Path(fresh['record']['identity']['python']), fresh['record']['identity']['code'])
                record = dict(fresh['record'], state='REGISTERING')
                atomic_json(config['state'] / RECORD, record)
                try:
                    path = Path(record['unit'])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    durable_write(checked_local_path(path), fresh['definition'].encode())
                    child.systemd_user(['daemon-reload'])
                    owned_state(record)
                    record['state'] = 'READY'
                    atomic_json(config['state'] / RECORD, record)
                    return dict(outcome='REGISTERED', **status(config))
                except Exception as exc:
                    record.update(state='PARTIAL', error=type(exc).__name__)
                    atomic_json(config['state'] / RECORD, record)
                    return dict(outcome='PARTIAL', ok=False, unit=record['unit'],
                                next_action='Inspect the unit and service.json; explicitly unregister, never auto-retry')
            record = registration(config)
            record['state'] = 'REMOVING'
            atomic_json(config['state'] / RECORD, record)
            path = checked_unit(record, allow_missing=True)
            if path.exists():
                remove_owned_launcher(path, record['unit_sha256'], path.parent)
            child.systemd_user(['daemon-reload'])
            state = manager_state(record['name'])
            if state['LoadState'] != 'not-found' or state['ActiveState'] not in ('inactive', 'failed'):
                raise RuntimeError('Removal outcome is unconfirmed; retain the registration and inspect')
            marker = checked_local_path(config['state'] / RECORD)
            remove_owned_launcher(marker, digest(marker.read_bytes()), config['state'])
            return dict(outcome='UNREGISTERED', backend='manual', retained='Project files, queue, answers and usage')


def start(config, timeout=10):
    adapter = launchd_backend(config)
    if adapter:
        return adapter.start(config, timeout)
    from . import runner_manager
    runner_manager.validate_timeout(timeout)
    record = registration(config)
    if not record:
        raise ValueError('Service registration missing')
    with FileLock(config['state'] / 'runner-management.lock'):
        guard_execution(config, BACKEND, record['id'])
        state = owned_state(record)
        observed = runner_manager.inspect(config)
        lifecycle = observed['management'] or {}
        if observed['lock_held']:
            if (lifecycle.get('backend') == BACKEND and lifecycle.get('service_id') == record['id']
                    and lifecycle.get('mode') == 'RUNNING' and observed['heartbeat_status'] == 'FRESH'
                    and state['ActiveState'] == 'active'):
                return runner_manager.response('REUSED', lifecycle['run_id'], backend=BACKEND), 0
            raise ValueError('Existing runner cannot be reused; inspect service status')
        if state['ActiveState'] not in ('inactive', 'failed'):
            raise ValueError('Service is starting or stopping; inspect instead of retrying')
        child.systemd_user(['start', '--no-block', record['name']])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observed = runner_manager.inspect(config)
            lifecycle = observed['management'] or {}
            if (observed['lock_held'] and lifecycle.get('backend') == BACKEND
                    and lifecycle.get('service_id') == record['id'] and lifecycle.get('mode') == 'RUNNING'):
                return runner_manager.response('STARTED', lifecycle['run_id'], True, backend=BACKEND), 0
            time.sleep(.05)
        return runner_manager.response('START_UNCONFIRMED', accepted=True, backend=BACKEND,
                                       next_step='Inspect systemctl --user status; no automatic retry'), 2


def stop(config, run_id, cancel=False, timeout=10):
    adapter = launchd_backend(config)
    if adapter:
        return adapter.stop(config, run_id, cancel, timeout)
    from . import runner_manager
    with FileLock(config['state'] / 'runner-management.lock'):
        record = registration(config)
        if not record:
            raise ValueError('Service registration missing')
        owned_state(record, partial=record['state'] != 'READY')
        lifecycle = runner_manager.inspect(config)['management'] or {}
        if lifecycle.get('backend') != BACKEND or lifecycle.get('service_id') != record['id'] or lifecycle.get('run_id') != run_id:
            raise ValueError('Observed runner does not belong to this service')
        result, code = runner_manager.stop(config, run_id, cancel, timeout, _service=True, _lock_held=True)
        if code == 0:
            current = runner_manager.inspect(config)
            if current['lock_held'] or (current['management'] or {}).get('run_id') != run_id:
                raise ValueError('Runner changed after stop acknowledgement; inspect before stopping the unit')
            child.systemd_user(['stop', record['name']])
        return dict(result, backend=BACKEND), code


def add_arguments(parser):
    sub = parser.add_subparsers(dest='service_action', required=True)
    sub.add_parser('status', help='Observe owned service and automatic start state')
    for action in ('register', 'enable', 'disable', 'unregister', 'start', 'stop'):
        item = sub.add_parser(action, help='Preview or explicitly apply ' + action)
        item.add_argument('--apply', action='store_true')
        if action in ('start', 'stop'):
            item.add_argument('--timeout', type=float, default=10)
        else:
            item.add_argument('--expect-plan', help='Exact plan_token returned by preview')
        if action == 'stop':
            item.add_argument('--run-id')
            item.add_argument('--cancel-active', action='store_true')
    internal = sub.add_parser('run', help='Foreground entry for the owned service definition')
    internal.add_argument('--registration-id', required=True)


def launchd_backend(config):
    record = registration(config)
    if (record and record['backend'] == LAUNCHD) or (record is None and sys.platform == 'darwin'):
        from . import launchd_service
        return launchd_service
    return None


def execute(args):
    from . import runner_manager
    config = runner_manager.checked_config(args.config)
    action = args.service_action
    code = 0
    if action == 'run':
        from .runner import run
        record = registration(config)
        if not record:
            raise ValueError('Service registration missing')
        return run(config, backend=record['backend'], service_id=args.registration_id)
    if action == 'status':
        result = status(config)
    elif action in ('start', 'stop'):
        if not args.apply:
            result = dict(status(config), action=action, runner=runner_manager.inspect(config),
                          pending_work_may_run=action == 'start', apply=False)
        elif action == 'start':
            result, code = start(config, args.timeout)
        else:
            if not args.run_id:
                raise ValueError('Service stop requires --run-id')
            result, code = stop(config, args.run_id, args.cancel_active, args.timeout)
    else:
        result = apply(config, action, args.expect_plan) if args.apply else preview(config, action)
        if result.get('ok') is False:
            code = 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return code
