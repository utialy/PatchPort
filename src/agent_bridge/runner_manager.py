"""Explicit local runner lifecycle operations; no PID-based termination."""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import threading
import uuid
import contextlib

from . import health
from .storage import FileLock, Store, load_config, readonly_database, atomic_json, canonical_selected_path

_CHILDREN = {}


def reap(run_id, proc):
    proc.wait()
    _CHILDREN.pop(run_id, None)


def safe_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked management path is not supported: ' + str(item))
    return path.resolve()


def execution_identity(config):
    path = config.get('config_path')
    fingerprint = hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None
    if config.get('_source_sha256', fingerprint) != fingerprint:
        raise ValueError('Configuration changed after loading')
    return dict(config=str(path) if path else None,
                config_sha256=fingerprint,
                root=str(config.get('root', config['state'].parent)), state=str(config['state']),
                python=str(Path(sys.executable).resolve()), code=health.code_identity())


def checked_config(path):
    if os.name == 'posix':
        path = Path(path).absolute()
        path = canonical_selected_path(path.parent) / path.name
    path = safe_path(path)
    data = path.read_bytes()
    raw = json.loads(data.decode('utf-8-sig'))
    config = load_config(path, data=raw)
    config['_source_sha256'] = hashlib.sha256(data).hexdigest()
    execution_identity(config)
    safe_path(config['root'])
    safe_path(config['state'])
    # Check the unresolved state path too; load_config canonicalizes it.
    safe_path(config['root'] / raw.get('state', '.agent-bridge'))
    for name in ('runner.lock', 'runner-management.lock', 'queue.sqlite3', 'health.json', 'runner-logs', 'runner-launch.json'):
        safe_path(config['state'] / name)
    return config


def inspect(config):
    result = health.inspect(config['state'], readonly=True)
    from .service_manager import registration
    service = registration(config)
    result.update(backend=service['backend'] if service else 'manual',
                  service={k: service[k] for k in ('name', 'state', 'unit')} if service else None)
    result.update(identity=execution_identity(config), management=None, control=None,
                  counts=None, problems=[], next_action='Inspect status before applying changes')
    path = config['state'] / 'queue.sqlite3'
    if path.exists():
        try:
            db = readonly_database(path)
            try:
                result['counts'] = dict(db.execute('SELECT state,COUNT(*) FROM tasks GROUP BY state'))
                control = dict(db.execute('SELECT * FROM control WHERE id=1').fetchone())
                control['paused'] = bool(control['paused'])
                control['remaining'] = (None if control['max_calls'] is None else
                                        max(0, control['max_calls'] - control['calls_started']))
                control['dispatch'] = ('PAUSED' if control['paused'] else
                                       'LIMIT_REACHED' if control['remaining'] == 0 else 'READY')
                result['control'] = control
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='runner_lifecycle'").fetchone():
                    row = db.execute('SELECT data FROM runner_lifecycle WHERE id=1').fetchone()
                    if row:
                        record = json.loads(row[0])
                        if (not isinstance(record, dict) or record.get('protocol') != 1
                                or not isinstance(record.get('identity'), dict)
                                or not isinstance(record.get('run_id'), str)
                                or record.get('mode') not in ('RUNNING', 'DRAINING', 'CANCELLING', 'STOPPED')):
                            raise ValueError('Invalid lifecycle record')
                        result['management'] = record
            finally:
                db.close()
        except (OSError, ValueError, sqlite3.Error, TypeError) as exc:
            result.update(management=None, control=None, counts=None)
            result['problems'].append('DATABASE_UNKNOWN')
            result['database_error'] = str(exc)
    record = result['management']
    receipt = config['state'] / 'runner-launch.json'
    if receipt.exists():
        try:
            receipt_bytes = receipt.read_bytes()
            launch = json.loads(receipt_bytes)
            result['launch'] = launch
            if not isinstance(launch, dict): raise ValueError('Invalid launch receipt')
            confirmed = record and record.get('confirmed_launch_sha256') == hashlib.sha256(receipt_bytes).hexdigest()
            if launch.get('outcome') != 'FAILED' and (not record or launch.get('run_id') != record['run_id']) and not confirmed:
                result['problems'].append('LAUNCH_UNCONFIRMED')
        except (OSError, ValueError):
            result['problems'].append('LAUNCH_UNKNOWN')
    result['identity_match'] = bool(record and record['identity'] == result['identity'])
    if result['control'] and result['control']['dispatch'] != 'READY':
        result['problems'].append(result['control']['dispatch'])
        if result['control']['paused'] and result['control']['remaining'] == 0:
            result['problems'].append('LIMIT_REACHED')
    if result['lock_held']:
        if not record: result['problems'].append('UNMANAGED')
        elif not result['identity_match']: result['problems'].append('IDENTITY_MISMATCH')
        if result['heartbeat_status'] != 'FRESH': result['problems'].append(result['heartbeat_status'])
        if result['restart_required']: result['problems'].append('RESTART_REQUIRED')
    result['pending_work_may_run'] = True
    return result


def validate_timeout(timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout must be finite and positive')


def response(outcome, run_id=None, accepted=False, **extra):
    return dict(outcome=outcome, run_id=run_id, request_accepted=accepted,
                next_action='Use runner status; never retry tasks automatically', **extra)


def start(config, timeout=10):
    validate_timeout(timeout)
    from .service_manager import registration, start as service_start
    if registration(config):
        return service_start(config, timeout)
    identity = execution_identity(config)
    if identity['code']['code_sha256'] is None:
        raise ValueError('Runtime code identity is unknown')
    manifest = Path(config['config_path']).parent / '.bridge-integration.json'
    if manifest.exists():
        safe_path(manifest)
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if Path(value['python']).resolve() != Path(sys.executable).resolve():
            raise ValueError('Connected Python differs; use the project launcher or reconnect')
    with FileLock(config['state'] / 'runner-management.lock'):
        checked = checked_config(config['config_path'])
        if execution_identity(checked) != identity:
            raise ValueError('Configuration changed before start')
        status = inspect(checked)
        if status['lock_held']:
            record = status['management']
            beat = status['health']
            if (status['identity_match'] and status['heartbeat_status'] == 'FRESH'
                    and isinstance(beat, dict) and record['mode'] == 'RUNNING'
                    and beat.get('management', {}).get('run_id') == record['run_id']):
                return response('REUSED', record['run_id']), 0
            raise ValueError('Existing runner cannot be reused; inspect runner status')
        if 'DATABASE_UNKNOWN' in status['problems']:
            raise ValueError('Database state is unknown; inspect before starting')
        if 'LAUNCH_UNKNOWN' in status['problems']:
            raise ValueError('Launch receipt is invalid; inspect before starting')
        receipt = checked['state'] / 'runner-launch.json'
        if receipt.exists():
            receipt_bytes = receipt.read_bytes()
            previous = json.loads(receipt_bytes)
            registered = status['management']
            confirmed = registered and registered.get('confirmed_launch_sha256') == hashlib.sha256(receipt_bytes).hexdigest()
            if (previous.get('outcome') != 'FAILED'
                    and (not registered or previous.get('run_id') != registered['run_id']) and not confirmed):
                raise ValueError('Previous launch is unconfirmed; inspect its log before another start')
        run_id = uuid.uuid4().hex
        logs = checked['state'] / 'runner-logs'
        logs.mkdir(exist_ok=True)
        expected = dict(run_id=run_id, identity=identity)
        from .child import start_background
        env = dict(os.environ)
        env['PYTHONUTF8'] = '1'
        # Preserve the explicitly selected runtime, including source installations.
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        atomic_json(receipt, dict(run_id=run_id, outcome='PENDING'))
        try:
            proc = start_background([sys.executable, '-m', 'agent_bridge.runner_manager',
                                     str(checked['config_path']), json.dumps(expected)],
                                    str(checked['root']), logs / (run_id + '.log'), env)
        except OSError:
            atomic_json(receipt, dict(run_id=run_id, outcome='FAILED'))
            raise
        _CHILDREN[run_id] = proc
        threading.Thread(target=reap, args=(run_id, proc), daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = inspect(checked)
            record = status['management']
            beat = status['health']
            if (record and record['run_id'] == run_id and record['mode'] == 'RUNNING' and status['lock_held']
                    and isinstance(beat, dict) and beat.get('management', {}).get('run_id') == run_id):
                return response('STARTED', run_id, True), 0
            if proc.poll() is not None:
                atomic_json(receipt, dict(run_id=run_id, outcome='FAILED'))
                return response('START_FAILED', run_id, True, exit_code=proc.returncode), 1
            time.sleep(.05)
        return response('START_UNCONFIRMED', run_id, True), 2


def stop(config, run_id, cancel=False, timeout=10, *, _service=False, _lock_held=False):
    validate_timeout(timeout)
    if not run_id: raise ValueError('stop --apply requires --run-id')
    if not _service:
        from .service_manager import registration, stop as service_stop
        if registration(config):
            return service_stop(config, run_id, cancel, timeout)
    with (contextlib.nullcontext() if _lock_held else FileLock(config['state'] / 'runner-management.lock')):
        config = checked_config(config['config_path'])
        status = inspect(config)
        record = status['management']
        if not record or record['run_id'] != run_id:
            raise ValueError('Runner identity changed or is unknown')
        if any(record['identity'].get(key) != status['identity'][key] for key in ('config', 'root', 'state')):
            raise ValueError('Runner target path differs')
        if record['mode'] == 'STOPPED' and not status['lock_held']:
            return response('STOPPED', run_id), 0
        beat = status['health']
        if (not status['lock_held'] or not isinstance(beat, dict)
                or beat.get('management', {}).get('run_id') != run_id):
            raise ValueError('Runner ownership cannot be confirmed')
        store = Store(config['state'])
        store.runner_transition(run_id, mode='CANCELLING' if cancel else 'DRAINING')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = inspect(config)
            record = status['management']
            if record and record['run_id'] == run_id and record['mode'] == 'STOPPED' and not status['lock_held']:
                proc = _CHILDREN.get(run_id)
                if proc is not None:
                    try:
                        proc.wait(timeout=max(.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        return response('STOP_PENDING', run_id, True), 2
                return response('STOPPED', run_id, True), 0
            time.sleep(.05)
        return response('STOP_PENDING', run_id, True), 2


def add_arguments(parser):
    commands = parser.add_subparsers(dest='runner_action', required=True)
    commands.add_parser('status', help='Observe runner state without creating a queue')
    for name in ('start', 'stop'):
        command = commands.add_parser(name, help='Preview or explicitly apply runner lifecycle changes')
        command.add_argument('--apply', action='store_true')
        command.add_argument('--timeout', type=float, default=10)
        if name == 'stop':
            command.add_argument('--run-id')
            command.add_argument('--cancel-active', action='store_true')


def execute(args):
    config = checked_config(args.config)
    if args.runner_action == 'status' or not args.apply:
        result, code = inspect(config), 0
        result['action'] = args.runner_action
        if args.runner_action == 'stop': result['cancel_active'] = args.cancel_active
    elif args.runner_action == 'start': result, code = start(config, args.timeout)
    else: result, code = stop(config, args.run_id, args.cancel_active, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    from .runner import run
    raise SystemExit(run(checked_config(sys.argv[1]), expected=json.loads(sys.argv[2])))
