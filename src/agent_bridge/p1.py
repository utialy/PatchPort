"""Reviewable, one-use management plans for the existing batch and role runtime."""
import copy
import importlib
import json
import re
from pathlib import Path
import secrets
import sys
import threading
import time

from . import child, context, runner_manager, setup_project, workspace
from .storage import FileLock, Store, atomic_json, identifier, readonly_database

ACTIONS = {
    'context.inventory': ({'config'}, {'prompt'}),
    'batch.preview': ({'config', 'batch_id', 'targets', 'prompt', 'plan'}, set()),
    'proposal.inspect': ({'config', 'task_id'}, {'path'}),
    'proposal.preview': ({'config', 'task_id', 'paths'}, set()),
    'recovery.preview': ({'config', 'task_id', 'backup'}, set()),
    'roles.overview': ({'config'}, set()),
    'roles.preview': ({'config', 'flow_id', 'prompt'}, set()),
    'roles.inspect': ({'config', 'flow_id', 'role'}, {'evidence_path', 'evidence_source', 'path'}),
    'roles.log': ({'config', 'flow_id', 'log'}, set()),
    'roles.promote.preview': ({'config', 'flow_id', 'role', 'task_id', 'paths'}, set()),
    'roles.recover.preview': ({'config', 'flow_id', 'role', 'task_id', 'backup'}, set()),
    'roles.prune.preview': ({'config', 'flow_id', 'role', 'task_id', 'days'}, set()),
    'archive.preview': ({'config', 'flow_id', 'days'}, set()),
    'archive.resume.preview': ({'config', 'flow_id', 'operation'}, set()),
    'operation.apply': ({'plan_id', 'apply'}, set()),
    'operation.cancel': ({'plan_id'}, set()),
}
HELPERS = {'role_flow', 'role_review', 'role_manage', 'project_overview', 'flow_cleanup', 'flow_archive_writer'}


def helper(name):
    """Load only fixed, bundled helper modules, never project-provided scripts."""
    if name not in HELPERS:
        raise ValueError('Unknown workflow helper')
    bundled = Path(__file__).parent / '_connect_assets' / 'tools'
    source = bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / 'tools'
    if not (source / (name + '.py')).is_file():
        raise ValueError('Workflow runtime assets are missing')
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    module = importlib.import_module(name)
    if Path(module.__file__).resolve().parent != source.resolve():
        raise ValueError('Workflow helper identity mismatch')
    return module


def validate(action, args):
    for key in ('prompt', 'batch_id', 'flow_id', 'backup', 'role', 'operation', 'path', 'evidence_path', 'evidence_source'):
        if key in args and (not isinstance(args[key], str) or '\x00' in args[key]):
            raise ValueError('Invalid text argument')
    for key in ('targets', 'paths'):
        if key in args and (not isinstance(args[key], list) or not args[key]
                            or any(not isinstance(v, str) or not v for v in args[key])
                            or len(args[key]) != len(set(args[key]))):
            raise ValueError('Select unique nonempty values')
    for key in ('batch_id', 'flow_id', 'backup', 'operation'):
        if key in args:
            identifier(args[key])
    if 'role' in args and args['role'] not in ('developer', 'reviewer'):
        raise ValueError('Invalid role')
    if 'days' in args and (type(args['days']) is not int or not 1 <= args['days'] <= 3650):
        raise ValueError('Invalid retention period')


def save_plan(session, action, args, config, report, **extra):
    from .management import MAX_PLANS, PLAN_SECONDS, fail
    if len(session.plans) >= MAX_PLANS:
        fail('PLAN_CAPACITY', 'Cancel an unused plan or preview after expiry')
    plan_id = secrets.token_urlsafe(32)
    if action in ('roles.promote.preview', 'roles.recover.preview', 'roles.prune.preview'):
        record = helper('role_review').resolve(config['root'], args['flow_id'], args['role'])
        report['workspace'] = str(record['home'])
        if action == 'roles.prune.preview':
            extra['prune_snapshot'] = workspace.files(record['home'])
    payload = dict(p1=True, action=action, args=copy.deepcopy(args), config=copy.deepcopy(config),
                   report=copy.deepcopy(report), **extra)
    session.plans[plan_id] = (time.monotonic() + PLAN_SECONDS, payload)
    return dict(plan_id=plan_id, expires_in=PLAN_SECONDS, report=report)


def task_record(config, task_id):
    identifier(task_id)
    db = readonly_database(config['state'] / 'queue.sqlite3')
    try:
        row = db.execute('SELECT state,result FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or row['state'] not in ('DONE', 'ERROR', 'FAILED', 'INTERRUPTED'):
            raise ValueError('Inspect a terminal task')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='role_tasks'").fetchone():
            if db.execute('SELECT 1 FROM role_tasks WHERE id=?', (task_id,)).fetchone():
                raise ValueError('Use role inspection and promotion for role tasks')
        result = json.loads(row['result'] or '{}')
        home = workspace.safe(config['state'], 'workspaces/' + task_id)
        if result.get('workspace') != str(home):
            raise ValueError('Task workspace identity mismatch')
        manifest = json.loads(workspace.safe(home, 'changes.json').read_text(encoding='utf-8'))
        if manifest != result.get('changes'):
            raise ValueError('Manifest differs from the terminal task')
        return home, manifest
    finally:
        db.close()


def file_diff(config, home, changes, relative):
    matches = [c for c in changes if c.get('path') == relative]
    if len(matches) != 1:
        raise ValueError('Select a changed path')
    change = matches[0]
    before = workspace.safe(config['root'], relative)
    after = workspace.safe(home / 'project', relative)
    if workspace.digest(after) != change['after']:
        raise ValueError('Proposal changed')
    result = dict(path=relative, original_matches=workspace.digest(before) == change['before'],
                  before=None, after=None, binary=False, too_large=False)
    for key, path in (('before', before), ('after', after)):
        if not path.exists():
            result[key] = ''
            continue
        if path.stat().st_size > 192 * 1024:
            result['too_large'] = True
            continue
        data = path.read_bytes()
        try:
            text = data.decode('utf-8')
            if '\x00' in text:
                raise UnicodeError('Binary file')
            result[key] = text
        except UnicodeError:
            result['binary'] = True
    return result


def proposal(config, task_id, paths=None, relative=None):
    home, manifest = task_record(config, task_id)
    for item in manifest:
        rel = item['path']
        item = dict(item)
        if workspace.digest(workspace.safe(home / 'project', rel)) != item['after']:
            raise ValueError('Proposal changed')
    if paths is not None:
        selected = []
        for rel in paths:
            matches = [c for c in manifest if c['path'] == rel and c['allowed']]
            if len(matches) != 1 or not any(rel == p or rel.startswith(p.rstrip('/') + '/') for p in config['writable']):
                raise ValueError('Select an allowed manifest path')
            if workspace.digest(workspace.safe(config['root'], rel)) != matches[0]['before']:
                raise ValueError('Original changed')
            selected.extend(matches)
        manifest = selected
    backups = []
    for path in sorted(home.glob('backup-*')):
        journal = workspace.safe(home, path.name + '/journal.json')
        if journal.is_file():
            data = json.loads(journal.read_text(encoding='utf-8'))
            backups.append(dict(backup=path.name, state=data.get('state')))
    view = dict(task=task_id, workspace=str(home), changes=manifest, backups=backups, read_only=True)
    if relative is not None:
        view['diff'] = file_diff(config, home, manifest, relative)
    return view


def role_config(config):
    if Path(config['config_path']) != config['root'] / 'bridge.json':
        raise ValueError('Role workflows require project bridge.json')


def role_start_report(config, args):
    role_config(config)
    setup_project.selected_snapshot(config['root'], config['include'])
    project = config['root']
    if workspace.safe(project, '.role-flows/' + args['flow_id']).exists():
        raise ValueError('Flow ID already exists; select a new ID')
    policy_path = workspace.safe(project, 'role_workflow.json')
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    from .runner import argv_for
    from .role_policy import readonly
    if set(policy) != {'developer', 'reviewer', 'checks'} or not readonly(policy['reviewer']):
        raise ValueError('Use an explicit read-only reviewer and test policy')
    checks = policy['checks']
    if not isinstance(checks, list) or not checks or any(not isinstance(c, list) or not c or
            any(not isinstance(v, str) or not v for v in c) for c in checks):
        raise ValueError('Explicit test command arrays are required')
    for role in ('developer', 'reviewer'):
        argv_for(policy[role], project / 'unused-reply.txt')
    inventory = context.preview(config, args['prompt'])
    context.enforce(inventory)
    if inventory['missing']:
        raise ValueError('Missing role input')
    return dict(flow=args['flow_id'], max_provider_calls=2, checks=checks,
                policy_sha256=workspace.digest(policy_path), input=inventory, automatic_promotion=False)


def start_role(config, args, expected):
    if role_start_report(config, args) != expected:
        raise ValueError('Role input or policy changed; preview again')
    root = workspace.safe(config['state'], 'p1-jobs')
    root.mkdir(parents=True, exist_ok=True)
    flow_id = args['flow_id']
    receipt = workspace.safe(root, flow_id + '.json')
    with FileLock(root / 'start.lock'):
        if receipt.exists():
            raise ValueError('Flow launch already recorded; inspect before requesting a new ID')
        prompt = workspace.safe(root, flow_id + '.txt')
        with prompt.open('x', encoding='utf-8') as stream:
            stream.write(args['prompt'])
        ticket = dict(flow=flow_id, state='STARTING', max_provider_calls=2, created=time.time(),
                      expected=expected, config=str(config['config_path']), launch_nonce=secrets.token_urlsafe(32))
        atomic_json(receipt, ticket)
        helper('role_flow')
        with child.external_environment() as env:
            env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            process = child.start_background([sys.executable, '-B', '-X', 'utf8', '-m', 'agent_bridge.role_job',
                                              '--config', str(config['config_path']), '--id', flow_id, '--nonce', ticket['launch_nonce']],
                                              config['root'], root / (flow_id + '.log'), env)
        atomic_json(receipt, dict(ticket, state='ACCEPTED', pid=process.pid, automatic_retry=False))
        threading.Thread(target=process.wait, daemon=True).start()
        return dict(flow=flow_id, state='ACCEPTED', max_provider_calls=2, automatic_retry=False)


def dispatch(session, action, args):
    from .management import fail
    validate(action, args)
    if action in ('operation.apply', 'operation.cancel'):
        item = session.plans.get(args['plan_id'])
        if item is None or not item[1].get('p1'):
            fail('PLAN_EXPIRED', 'Preview again in this core session')
        _, payload = session.plans.pop(args['plan_id'])
        if action == 'operation.cancel':
            return dict(cancelled=True)
        config = runner_manager.checked_config(payload['args']['config'])
        if config != payload['config']:
            raise ValueError('Config changed; preview again')
        return apply_plan(config, payload)
    config = runner_manager.checked_config(args['config'])
    if action == 'context.inventory':
        setup_project.selected_snapshot(config['root'], config['include'])
        report = context.preview(config, args.get('prompt', ''))
        report['required'] = context._required(config, report['files'], automatic=True)
        report['endpoints'] = list(config['endpoints'])
        return report
    if action == 'batch.preview':
        setup_project.selected_snapshot(config['root'], config['include'])
        if any(t not in config['endpoints'] for t in args['targets']) or not args['prompt'].strip():
            raise ValueError('Select known endpoints and a nonempty prompt')
        inventory = context.plan_preview(config, args['prompt'], plan=args['plan'])
        context.enforce(inventory)
        saved = context.freeze_plan(config, args['prompt'], inventory, args['targets'])
        return save_plan(session, action, args, config,
                         dict(batch=args['batch_id'], targets=args['targets'], context=inventory, starts_runner=False), saved=saved)
    if action == 'proposal.inspect':
        return proposal(config, args['task_id'], relative=args.get('path'))
    if action == 'proposal.preview':
        report = proposal(config, args['task_id'], args['paths'])
    elif action == 'recovery.preview':
        task_record(config, args['task_id'])
        report = workspace.recover(config, args['task_id'], args['backup'])
    else:
        role_config(config)
        project = config['root']
        if action == 'roles.overview':
            view = helper('project_overview').overview(project)
            jobs = workspace.safe(config['state'], 'p1-jobs')
            view['launches'] = [{k:v for k,v in json.loads(workspace.safe(jobs, p.name).read_text(encoding='utf-8')).items()
                                if k not in ('expected', 'config', 'launch_nonce')} for p in sorted(jobs.glob('*.json'))[-100:]] if jobs.exists() else []
            return view
        if action == 'roles.inspect':
            view = helper('role_review').inspect(project, args['flow_id'], args['role'],
                       evidence_path=args.get('evidence_path'), evidence_source=args.get('evidence_source'))
            if 'path' in args:
                record = helper('role_review').resolve(project, args['flow_id'], args['role'])
                view['diff'] = file_diff(record['config'], record['home'], view['changes'], args['path'])
            if 'workspace' in view:
                home = Path(view['workspace'])
                view['backups'] = [dict(backup=p.name) for p in sorted(home.glob('backup-*'))]
            return view
        if action == 'roles.log':
            name = args['log']
            if not isinstance(name, str) or not re.fullmatch(r'test-[0-9]+\.log', name):
                raise ValueError('Select an explicit test log')
            _, _, flow, saved = helper('role_review').flow_header(project, args['flow_id'])
            if saved.get('state') == 'ARCHIVED' or 'artifact_layout' in saved:
                archive = helper('role_review').inspect(project, args['flow_id'], 'developer', evidence_path=name, evidence_source='flow_metadata')
                return dict(flow=args['flow_id'], log=name, evidence=archive['evidence'])
            log = workspace.safe(flow, name)
            if log.stat().st_size > 256 * 1024:
                raise ValueError('Log too large; inspect the saved file directly')
            return dict(flow=args['flow_id'], log=name, text=log.read_bytes().decode('utf-8', errors='replace'))
        if action == 'roles.preview':
            report = role_start_report(config, args)
        elif action == 'roles.promote.preview':
            report = helper('role_manage').promote(project, args['flow_id'], args['role'], args['task_id'], args['paths'])
        elif action == 'roles.recover.preview':
            report = helper('role_manage').recover(project, args['flow_id'], args['role'], args['task_id'], args['backup'])
        elif action == 'roles.prune.preview':
            report = helper('role_manage').prune(project, args['flow_id'], args['role'], args['task_id'], args['days'])
            if not report['artifact']['eligible']:
                return dict(plan_id=None, report=report)
        elif action == 'archive.preview':
            report = helper('flow_cleanup').preview(project, args['flow_id'], args['days'])
            if not report['eligible']:
                return dict(plan_id=None, report=report)
        elif action == 'archive.resume.preview':
            report = helper('flow_archive_writer').continue_archive(project, args['flow_id'], args['operation'])
        else:
            raise ValueError('Unsupported P1 action')
    return save_plan(session, action, args, config, report)


def apply_plan(config, payload):
    action, args, report = (payload[k] for k in ('action', 'args', 'report'))
    if action == 'batch.preview':
        setup_project.selected_snapshot(config['root'], config['include'])
        for target in args['targets']:
            context.check_plan(config, args['prompt'], payload['saved'], target)
        tasks = Store(config['state']).submit(args['batch_id'], args['targets'], args['prompt'], config['endpoints'], payload['saved'])
        return dict(tasks=tasks, batch=args['batch_id'], submitted=True, starts_runner=False)
    if action == 'proposal.preview':
        if proposal(config, args['task_id'], args['paths']) != report:
            raise ValueError('Proposal changed; preview again')
        with FileLock(config['state'] / 'runner.lock'):
            return workspace.review(config, args['task_id'], args['paths'])
    if action == 'recovery.preview':
        task_record(config, args['task_id'])
        if workspace.recover(config, args['task_id'], args['backup']) != report:
            raise ValueError('Recovery journal changed')
        with FileLock(config['state'] / 'runner.lock'):
            return workspace.recover(config, args['task_id'], args['backup'], True)
    project = config['root']
    if action == 'roles.preview':
        return start_role(config, args, report)
    if action == 'roles.promote.preview':
        fresh = helper('role_manage').promote(project, args['flow_id'], args['role'], args['task_id'], args['paths'])
        fresh['workspace'] = str(helper('role_review').resolve(project, args['flow_id'], args['role'])['home'])
        if fresh != report:
            raise ValueError('Role proposal changed')
        return helper('role_manage').promote(project, args['flow_id'], args['role'], args['task_id'], args['paths'], True)
    if action == 'roles.recover.preview':
        fresh = helper('role_manage').recover(project, args['flow_id'], args['role'], args['task_id'], args['backup'])
        fresh['workspace'] = str(helper('role_review').resolve(project, args['flow_id'], args['role'])['home'])
        if fresh != report:
            raise ValueError('Role recovery changed')
        return helper('role_manage').recover(project, args['flow_id'], args['role'], args['task_id'], args['backup'], True)
    if action == 'roles.prune.preview':
        record = helper('role_review').resolve(project, args['flow_id'], args['role'])
        if workspace.files(record['home']) != payload['prune_snapshot']:
            raise ValueError('Role artifacts changed after cleanup preview')
        return helper('role_manage').prune(project, args['flow_id'], args['role'], args['task_id'], args['days'], True)
    if action == 'archive.preview':
        return helper('flow_archive_writer').write_archive(project, args['flow_id'], report['plan_hash'], args['days'])
    if action == 'archive.resume.preview':
        return helper('flow_archive_writer').continue_archive(project, args['flow_id'], args['operation'], report['plan_hash'], apply=True)
    raise ValueError('Unsupported operation')
