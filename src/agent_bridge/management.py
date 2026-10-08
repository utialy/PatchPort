"""Versioned local JSON-lines management; no shell or provider invocation API."""
import contextlib
import copy
import json
from pathlib import Path
import queue
import secrets
import sqlite3
import subprocess
import sys
import threading
import time

from . import answers, interactive, p1, runner_manager, setup_project, usage
from .storage import Store, Transaction, identifier, readonly_database

PROTOCOL = 1
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_REQUESTS = 4096
MAX_PLANS = 8
PLAN_SECONDS = 600
IDLE_SECONDS = 300
ACTIONS = {
    'capabilities': (set(), set()),
    'project.inspect': ({'project'}, {'config'}),
    'usage': ({'config'}, {'days', 'endpoint', 'batch', 'group_by'}),
    'interactive.initialize': ({'database', 'apply'}, set()),
    'interactive.begin': ({'database', 'apply', 'session_id', 'node', 'project', 'provider', 'name', 'owner'}, set()),
    'interactive.record': ({'database', 'apply', 'session_id', 'event_id', 'kind', 'data'}, {'turn'}),
    'interactive.delivery': ({'database', 'apply', 'owner', 'data'}, set()),
    'interactive.end': ({'database', 'apply', 'owner'}, set()),
    'interactive.report': ({'database', 'project'}, {'days', 'limit', 'owner'}),
    'interactive.event': ({'database', 'project', 'event_id'}, set()),
    'interactive.resume': ({'database', 'project', 'session_id'}, set()),
    'discover': ({'provider'}, set()),
    'setup.preview': ({'project'}, {'providers', 'include', 'writable', 'starter', 'create_project'}),
    'setup.apply': ({'plan_id', 'apply'}, set()),
    'setup.cancel': ({'plan_id'}, set()),
    'runner.status': ({'config'}, set()),
    'runner.start': ({'config', 'apply'}, {'timeout'}),
    'runner.stop': ({'config', 'run_id', 'apply'}, {'timeout', 'cancel_active'}),
    'pause': ({'config', 'apply'}, set()),
    'resume': ({'config', 'apply'}, set()),
    'limit': ({'config', 'max_calls', 'apply'}, set()),
    'result': ({'config', 'task_id'}, set()),
    'tasks.list': ({'config'}, {'limit'}),
}
ACTIONS.update(p1.ACTIONS)


class Problem(ValueError):
    def __init__(self, code, next_action):
        self.code, self.next_action = code, next_action
        super().__init__(code)


def fail(code, next_action='Review the request and use a new request ID'):
    raise Problem(code, next_action)


def response(request_id, *, result=None, code=None, next_action=None):
    return dict(protocol=PROTOCOL, id=request_id, ok=code is None, result=result,
                problem=None if code is None else dict(code=code, next_action=next_action))


def text_value(value):
    return isinstance(value, str) and bool(value.strip()) and '\x00' not in value


def validate_args(action, args):
    required, optional = ACTIONS[action]
    if not isinstance(args, dict) or not required <= args.keys() or args.keys() - required - optional:
        fail('INVALID_ARGUMENTS')
    for key in ('config', 'project', 'provider', 'plan_id', 'run_id', 'task_id'):
        if key in args and not text_value(args[key]):
            fail('INVALID_ARGUMENTS')
    for key in ('config', 'project', 'database'):
        if key in args and not Path(args[key]).is_absolute():
            fail('ABSOLUTE_PATH_REQUIRED')
    for key in ('starter', 'create_project', 'cancel_active', 'apply'):
        if key in args and type(args[key]) is not bool:
            fail('INVALID_ARGUMENTS')
    if 'apply' in required and args['apply'] is not True:
        fail('EXPLICIT_APPLY_REQUIRED')
    for key in ('include', 'writable'):
        if key in args and (not isinstance(args[key], list) or not args[key]
                            or any(not text_value(p) for p in args[key])):
            fail('INVALID_ARGUMENTS')
    if 'timeout' in args and (type(args['timeout']) not in (float, int)
                              or not 0 < args['timeout'] <= 30):
        fail('INVALID_ARGUMENTS')
    if action == 'tasks.list' and 'limit' in args:
        if type(args['limit']) is not int or not 1 <= args['limit'] <= 100:
            fail('INVALID_ARGUMENTS')
    if 'days' in args and (type(args['days']) is not int or not 1 <= args['days'] <= 3650):
        fail('INVALID_ARGUMENTS')
    if 'group_by' in args and args['group_by'] not in ('endpoint', 'model', 'batch', 'day'):
        fail('INVALID_ARGUMENTS')
    if action == 'limit' and args['max_calls'] is not None:
        if type(args['max_calls']) is not int or not 0 <= args['max_calls'] <= 2**63 - 1:
            fail('INVALID_ARGUMENTS')
    if action == 'discover' and args['provider'] not in setup_project.PROVIDERS:
        fail('INVALID_ARGUMENTS')
    if 'providers' in args:
        providers = args['providers']
        if not isinstance(providers, dict) or not providers:
            fail('INVALID_ARGUMENTS')
        for provider, selection in providers.items():
            if provider not in setup_project.PROVIDERS:
                fail('INVALID_ARGUMENTS')
            if isinstance(selection, dict):
                if set(selection) != {'command', 'launch'}:
                    fail('INVALID_ARGUMENTS')
                try:
                    setup_project.validate_launch(selection['launch'])
                except ValueError:
                    fail('INVALID_ARGUMENTS')
                if selection['launch'] is None:
                    fail('INVALID_ARGUMENTS')
                selection = selection['command']
            if (not text_value(selection) or not (Path(selection).is_absolute()
                                                 or setup_project.command_name(selection))):
                fail('INVALID_ARGUMENTS')


def visible_state(state):
    if state in ('PLAN_QUEUED', 'PLAN_RUNNING', 'ROLE_QUEUED', 'ROLE_RUNNING',
                 'SUMMARY_QUEUED', 'SUMMARY_RUNNING'):
        return state.split('_', 1)[1]
    return state


def list_tasks(config, limit=20):
    """Read bounded task metadata without initializing the queue or loading answers."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Task list limit must be an integer from 1 to 100')
    path = config['state'] / 'queue.sqlite3'
    if not path.exists():
        return dict(tasks=[], limit=limit, more=False)
    db = readonly_database(path)
    try:
        rows = db.execute('SELECT id,batch,endpoint,state,created,started,finished FROM tasks '
                          'ORDER BY created DESC,id DESC LIMIT ?', (limit + 1,)).fetchall()
        tasks = [dict(row) for row in rows[:limit]]
        for task in tasks:
            task['state'] = visible_state(task['state'])
        return dict(tasks=tasks, limit=limit, more=len(rows) > limit)
    finally:
        db.close()


def read_result(config, task_id):
    """Read one durable task result without initializing or migrating the queue."""
    identifier(task_id)
    path = config['state'] / 'queue.sqlite3'
    if not path.exists():
        fail('RESULT_NOT_FOUND', 'Select an existing task ID')
    db = readonly_database(path)
    try:
        row = db.execute('SELECT id,endpoint,state,result FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            fail('RESULT_NOT_FOUND', 'Select an existing task ID')
        row = dict(row)
        row['result'] = json.loads(row['result']) if row['result'] else {}
        row['state'] = visible_state(row['state'])
        row['task_success'] = 'NOT_EVALUATED'
        row['artifact_cleanup'] = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_cleanup'").fetchone():
            cleanup = db.execute('SELECT state,updated FROM artifact_cleanup WHERE id=?', (task_id,)).fetchone()
            row['artifact_cleanup'] = dict(cleanup) if cleanup else None
        return answers.concise(row)
    finally:
        db.close()


class Session:
    """Own plans and request IDs for exactly one sequential core connection."""
    def __init__(self):
        self.plans = {}
        self.seen = set()

    def handle(self, request):
        request_id = None
        try:
            if not isinstance(request, dict) or set(request) != {'protocol', 'id', 'action', 'args'}:
                fail('INVALID_REQUEST')
            if not text_value(request['id']) or len(request['id']) > 128:
                fail('INVALID_REQUEST_ID')
            request_id = request['id']
            if type(request['protocol']) is not int or request['protocol'] != PROTOCOL:
                fail('UNSUPPORTED_PROTOCOL')
            if request_id in self.seen:
                fail('DUPLICATE_REQUEST', 'Inspect the previous outcome; do not repeat a mutation automatically')
            if len(self.seen) >= MAX_REQUESTS:
                fail('SESSION_FULL', 'Open a new core session and preview again')
            self.seen.add(request_id)
            action = request['action']
            if not isinstance(action, str) or action not in ACTIONS:
                fail('UNSUPPORTED_ACTION')
            args = request['args']
            validate_args(action, args)
            self.plans = {key: item for key, item in self.plans.items() if item[0] > time.monotonic()}
            result = self.dispatch(action, args)
            if isinstance(result, dict) and result.get('ok') is False:
                return response(request_id, result=result, code='OPERATION_INCOMPLETE',
                                next_action='Review the result; preview again before another apply')
            return response(request_id, result=result)
        except Problem as exc:
            return response(request_id, code=exc.code, next_action=exc.next_action)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, OverflowError,
                sqlite3.Error, subprocess.SubprocessError):
            # Exceptions may contain config values or command output. Never echo them.
            return response(request_id, code='OPERATION_FAILED',
                            next_action='Inspect project state with the CLI; preview again before applying')

    def dispatch(self, action, args):
        if action in p1.ACTIONS:
            return p1.dispatch(self, action, args)
        if action == 'capabilities':
            from . import __version__
            return dict(core_version=__version__, python=sys.version.split()[0], protocol=PROTOCOL,
                        actions=sorted(ACTIONS), interactive_schema=interactive.SCHEMA, workflow_schema=1)
        if action == 'project.inspect':
            project = Path(args['project']).resolve(strict=True)
            path = Path(args.get('config', project / 'bridge.json'))
            if not path.exists():
                return dict(linked=False, project=str(project), config=str(path), database=None, endpoints={})
            config = runner_manager.checked_config(str(path))
            if config['root'] != project:
                fail('PROJECT_CONFIG_MISMATCH')
            database = config['state'] / 'queue.sqlite3'
            return dict(linked=True, project=str(project), config=str(path), state=str(config['state']),
                        database=str(database) if database.exists() else None,
                        endpoints=config['endpoints'], context_budget=config.get('context_budget', {}),
                        control=runner_manager.inspect(config))
        if action.startswith('interactive.'):
            path = args['database']
            if action == 'interactive.initialize':
                return interactive.initialize(path)
            if action == 'interactive.begin':
                return interactive.begin(path, **{key:args[key] for key in ('session_id', 'node', 'project', 'provider', 'name', 'owner')})
            if action == 'interactive.record':
                return interactive.record(path, **{key:args[key] for key in ('session_id', 'event_id', 'kind', 'data')}, turn=args.get('turn'))
            if action == 'interactive.delivery':
                return interactive.delivery(path, args['owner'], args['data'])
            if action == 'interactive.end':
                return interactive.end_owner(path, args['owner'])
            if action == 'interactive.event':
                return interactive.read_event(path, args['project'], args['event_id'])
            if action == 'interactive.resume':
                return interactive.resume_info(path, args['project'], args['session_id'])
            return interactive.report(path, args['project'], args.get('days'), args.get('limit', 50), args.get('owner'))
        if action == 'discover':
            return dict(candidates=setup_project.discover(args['provider']),
                        authentication='NOT_CHECKED', invocation='NOT_RUN')
        if action == 'setup.preview':
            if len(self.plans) >= MAX_PLANS:
                fail('PLAN_CAPACITY', 'Cancel an unused plan or wait for expiry')
            options = {k: v for k, v in args.items() if k not in ('project', 'providers')}
            if 'providers' in args:
                options['endpoints'] = {
                    p: setup_project.choose_endpoint(p, value['command'], False, value['launch'])
                    if isinstance(value, dict) else setup_project.choose_endpoint(p, value, False)
                    for p, value in args['providers'].items()}
            plan = setup_project.plan_setup(args['project'], python=Path(sys.executable), **options)
            plan_id = secrets.token_urlsafe(32)
            self.plans[plan_id] = (time.monotonic() + PLAN_SECONDS, plan)
            return dict(plan_id=plan_id, expires_in=PLAN_SECONDS, report=copy.deepcopy(plan['report']))
        if action in ('setup.apply', 'setup.cancel'):
            item = self.plans.get(args['plan_id'])
            if item is None or item[1].get('p1'):
                fail('PLAN_EXPIRED', 'Preview again in this core session')
            self.plans.pop(args['plan_id'])
            return (setup_project.apply_setup(item[1]) if action == 'setup.apply'
                    else dict(cancelled=True))
        config = runner_manager.checked_config(args['config'])
        if action == 'usage':
            path = config['state'] / 'queue.sqlite3'
            if not path.exists():
                return dict(summary=usage.totals([]), tasks=[], groups={}, group_by=args.get('group_by', 'endpoint'),
                            timezone='UTC', period_basis='task_started', unknown_representation='null', database_exists=False)
            class ReadonlyStore:
                def connect(self):
                    return Transaction(readonly_database(path))
            return dict(usage.report(ReadonlyStore(), args.get('days'), args.get('endpoint'),
                                     args.get('batch'), args.get('group_by', 'endpoint')), database_exists=True)
        if action == 'tasks.list':
            return list_tasks(config, args.get('limit', 20))
        if action == 'runner.status':
            status = runner_manager.inspect(config)
            status['diagnostics'] = setup_project.diagnostics(config['endpoints'])
            return status
        if action in ('runner.start', 'runner.stop'):
            if action == 'runner.start':
                result, code = runner_manager.start(config, args.get('timeout', 10))
            else:
                result, code = runner_manager.stop(config, args['run_id'], args.get('cancel_active', False),
                                                   args.get('timeout', 10))
            return dict(result, ok=code == 0)
        if action == 'result':
            return read_result(config, args['task_id'])
        store = Store(config['state'])
        if action == 'limit':
            store.set_limit(args['max_calls'])
        else:
            store.pause(action == 'pause')
        return store.control()


def decode(line):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('Nonfinite number')
    return json.loads(line.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def encode(reply):
    try:
        data = (json.dumps(reply, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        return encode(response(reply['id'], code='INVALID_RESULT',
                               next_action='Inspect stored data with the CLI; do not repeat mutations automatically'))
    if len(data) > MAX_RESPONSE:
        return encode(response(reply['id'], code='RESPONSE_TOO_LARGE',
                               next_action='Inspect the result with the CLI; do not repeat mutations automatically'))
    return data


def serve(source=None, target=None, *, idle_timeout=IDLE_SECONDS):
    """Serve inherited binary pipes; EOF or inactivity expires every saved plan."""
    # A daemon reader must not hold BufferedReader's lock during interpreter shutdown
    # or while subprocess duplicates standard streams on Windows.
    source = source if source is not None else sys.stdin.buffer.raw
    target = target if target is not None else sys.stdout.buffer
    inbox = queue.Queue(maxsize=1)
    def read():
        try:
            while True:
                line = source.readline(MAX_REQUEST + 1)
                inbox.put(line)
                if not line or len(line) > MAX_REQUEST:
                    return
        except OSError:
            inbox.put(b'')
    threading.Thread(target=read, daemon=True).start()
    session = Session()
    while True:
        try:
            line = inbox.get(timeout=idle_timeout)
        except queue.Empty:
            return 0
        if not line:
            return 0
        if len(line) > MAX_REQUEST:
            reply = response(None, code='REQUEST_TOO_LARGE', next_action='Open a new core session with smaller requests')
        else:
            try:
                request = decode(line)
            except (ValueError, UnicodeError, RecursionError):
                reply = response(None, code='INVALID_JSON', next_action='Send one UTF-8 JSON object per line')
            else:
                with contextlib.redirect_stdout(sys.stderr):
                    reply = session.handle(request)
        try:
            target.write(encode(reply))
            target.flush()
        except (BrokenPipeError, OSError):
            return 0
        if len(line) > MAX_REQUEST:
            return 1


if __name__ == '__main__':
    raise SystemExit(serve())
