"""Project setup with explicit selection, preview, and no provider execution."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import connect, workspace
from .runner import argv_for
from .storage import load_config


STARTER = 'PATCHPORT_START.md'
STARTER_BYTES = b'# Project\n\nDescribe the project and the first review request here.\n'
PROVIDERS = ('codex', 'claude')
NPM_ENTRY = {'codex': '@openai/codex/bin/codex.js',
             'claude': '@anthropic-ai/claude-code/cli.js'}
PRIVATE = {'.codex', '.claude', '.agents', '.ssh', '.aws', '.azure', '.gnupg',
           '.role-flows', '.bridge-integration-backups', 'auth.json', 'credentials.json',
           '.credentials.json', '.npmrc', '.pypirc', 'id_rsa', 'id_ed25519'}
INTEGRATION = {'bridge.json', 'role_workflow.json', connect.MANIFEST, 'bridge.py',
               'role_flow.py', 'role_review.py', 'role_manage.py', 'project_overview.py',
               'flow_archive.py', 'flow_lifecycle.py', 'flow_cleanup.py', 'flow_archive_writer.py'}


def sensitive(name):
    lower = name.casefold()
    return lower in PRIVATE or lower == '.env' or lower.startswith('.env.') or lower.endswith(('.pem', '.key'))


def command_path(path):
    """Resolve a selected executable or a JS entry point without executing it."""
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        raise ValueError('CLI file is missing; select an installed executable')
    if path.suffix.lower() == '.js':
        node = shutil.which('node')
        if not node:
            raise ValueError('Node is missing; install Node or select a native CLI executable')
        if os.name == 'nt' and Path(node).suffix.lower() != '.exe':
            raise ValueError('Select a native Node executable')
        return [str(Path(node).resolve()), str(path.resolve())]
    if os.name == 'nt' and path.suffix.lower() != '.exe':
        raise ValueError('Shell shims are unsupported; select a native executable or a CLI .js file with Node')
    if os.name != 'nt' and not os.access(path, os.X_OK):
        raise ValueError('CLI file is not executable')
    return [str(path.resolve())]


def discover(provider):
    """Inspect PATH and known user installation locations, never credentials."""
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider')
    directories = [Path(p.strip('"')) for p in os.get_exec_path() if p and Path(p.strip('"')).is_absolute()]
    directories.append(Path.home() / '.local' / 'bin')
    if os.environ.get('APPDATA'):
        directories.append(Path(os.environ['APPDATA']) / 'npm')
    names = [provider + suffix for suffix in ('.exe', '.cmd', '.bat', '.ps1')] if os.name == 'nt' else [provider]
    candidates, seen = [], set()
    for directory in dict.fromkeys(directories):
        for path in [*(directory / name for name in names), directory / 'node_modules' / NPM_ENTRY[provider]]:
            if not path.is_file():
                continue
            key = os.path.normcase(str(path.resolve()))
            if key in seen:
                continue
            seen.add(key)
            try:
                command = command_path(path)
                candidates.append(dict(path=str(path), command=command, status='FOUND'))
            except ValueError as exc:
                candidates.append(dict(path=str(path), status='UNSUPPORTED', next_action=str(exc)))
    return candidates


def selected_snapshot(project, paths, initial_files=None):
    """Check names before hashing selected files; never inspect secret contents."""
    initial_files = initial_files or {}
    result = {}

    def visit(path, relative):
        workspace.safe(project, relative)
        if any(sensitive(part) for part in Path(relative).parts):
            raise ValueError('Sensitive path is not eligible for setup input: ' + relative)
        if path.is_dir():
            result[relative + '/'] = None
            for child in sorted(path.iterdir()):
                if child.name in workspace.EXCLUDED:
                    continue
                visit(child, relative + '/' + child.name)
        elif path.is_file():
            result[relative] = workspace.digest(path)
        else:
            raise ValueError('Selected input is missing or unsupported: ' + relative)

    if not paths or len(set(paths)) != len(paths):
        raise ValueError('Select one or more distinct input paths')
    for relative in paths:
        path = workspace.safe(project, relative)
        if (any(part in workspace.EXCLUDED for part in Path(relative).parts)
                or relative.split('/')[0] in INTEGRATION or relative == 'tasks/request.md'):
            raise ValueError('Select project source files, not integration or runtime files')
        if relative in initial_files and not path.exists():
            result[relative] = None
        else:
            visit(path, relative)
    return result


def diagnostics(endpoints):
    checks = {}
    for name, endpoint in endpoints.items():
        try:
            command = argv_for(endpoint, Path('unused-reply.txt'))
            raw_command = endpoint['command']
            if len(raw_command) > 1 and raw_command[1].endswith('.js') and not Path(raw_command[1]).is_file():
                raise ValueError('CLI JS entry point is missing')
            checks[name] = dict(status='FOUND', executable=command[0])
        except (OSError, ValueError):
            checks[name] = dict(status='MISSING_OR_UNSUPPORTED',
                                next_action='Install the official CLI or select a supported executable path')
    return dict(commands=checks, authentication='NOT_CHECKED', invocation='NOT_RUN')


def plan_setup(project, *, python=None, endpoints=None, include=None, writable=None,
               starter=False, create_project=False):
    project = connect.project_path(project)
    config_path = workspace.safe(project, 'bridge.json')
    existing = config_path.exists()
    initial = {}
    if existing:
        if endpoints is not None or include is not None or writable is not None or starter:
            raise ValueError('Existing bridge.json is preserved; omit new configuration options')
        original_config = config_path.read_bytes()
        config = load_config(config_path, data=json.loads(original_config.decode('utf-8-sig')))
        if config['root'] != project:
            raise ValueError('bridge.json must refer to the selected project root')
        raw = None
    else:
        if not endpoints:
            raise ValueError('Select at least one installed provider')
        if starter:
            if (project / STARTER).exists():
                raise ValueError('Starter file already exists; select it as input instead')
            initial[STARTER] = STARTER_BYTES
        include = list(include or ([STARTER] if starter else []))
        if starter and STARTER not in include:
            raise ValueError('The starter file must be selected as input')
        writable = list(writable or include)
        if any(not any(p == q or p.startswith(q + '/') for q in include) for p in writable):
            raise ValueError('Writable paths must stay within the selected input paths')
        raw = dict(project='.', state='.agent-bridge', include=include, writable=writable,
                   parallel=1, timeout=600, endpoints=endpoints)
        config = raw
    snapshot = selected_snapshot(project, config['include'], initial)
    check = diagnostics(config['endpoints'])
    plan = connect.plan_install(project, python, config_data=raw, initial_files=initial,
                                create_project=create_project)
    if existing and plan['guards']['bridge.json'] != connect.digest(original_config):
        raise ValueError('Configuration changed while planning; review a new plan')
    # Installation must not change files that are also task inputs.
    destinations = {item['path'] for item in plan['actions']}
    if (set(snapshot) - set(initial)) & destinations:
        raise ValueError('Selected inputs overlap connection assets')
    planned_writes = [a['path'] for a in plan['actions'] if a['action'] != 'keep']
    if any(any(name == p or name.startswith(p.rstrip('/') + '/') for p in config['include'])
           for name in planned_writes if name not in initial):
        raise ValueError('Connection would add files inside the selected input scope')
    summary = connect.install(plan)
    summary.update(connection='PREVIEW' if summary['ok'] else 'CONFLICT',
                   create_project=not project.exists(), diagnostics=check,
                   include=list(config['include']), writable=list(config['writable']),
                   selected_rules=[p for p in snapshot if Path(p).name.lower() in {'agents.md', 'claude.md'}],
                   request_file='tasks/request.md',
                   next_action='Apply this plan, then authenticate in the official CLI. Start the runner separately.',
                   note='Writable paths only control explicit promotion to the original project. The copy is not a security sandbox.')
    summary['ok'] = summary['ok'] and bool(check['commands']) and all(c['status'] == 'FOUND' for c in check['commands'].values())
    if not summary['ok'] and summary['connection'] == 'PREVIEW':
        summary['connection'] = 'NEEDS_CLI'
    return dict(install=plan, report=summary, snapshot=snapshot, initial=initial)


def apply_setup(plan):
    if not plan['report']['ok']:
        return plan['report']
    def recheck():
        project = plan['install']['project']
        if connect.project_path(project) != project:
            raise ValueError('Project location changed after preview')
        for name, expected in plan['install']['guards'].items():
            if workspace.digest(workspace.safe(project, name)) != expected:
                raise ValueError('Configuration or manifest changed after preview: ' + name)
        actual = selected_snapshot(project, plan['report']['include'], plan['initial'])
        if actual != plan['snapshot']:
            raise ValueError('Selected inputs changed after preview; review a new plan')
        if diagnostics_from_plan(plan) != plan['report']['diagnostics']:
            raise ValueError('CLI resolution changed after preview')
    recheck()
    try:
        result = connect.install(plan['install'], True, before_write=recheck)
    except connect.InstallError as exc:
        return dict(plan['report'], **{**exc.report, 'connection': 'PARTIAL'})
    return dict(plan['report'], **{**result, 'connection': 'READY',
                                  'next_action': 'Authenticate in the official CLI if needed. Start the runner separately; no test request was sent.'})


def diagnostics_from_plan(plan):
    # Commands are frozen in generated contents or the guarded existing config.
    install = plan['install']
    data = install['contents'].get('bridge.json')
    if data is None:
        data = workspace.safe(install['project'], 'bridge.json').read_bytes()
    raw = json.loads(data.decode('utf-8-sig'))
    return diagnostics(raw['endpoints'])


def add_arguments(parser):
    parser.add_argument('--project', type=Path)
    parser.add_argument('--python', type=Path)
    parser.add_argument('--provider', action='append', choices=PROVIDERS)
    parser.add_argument('--codex', type=Path, help='Native executable or CLI JS entry point')
    parser.add_argument('--claude', type=Path, help='Native executable or CLI JS entry point')
    parser.add_argument('--include', action='append', help='Project-relative input path; repeat for more paths')
    parser.add_argument('--writable', action='append', help='Explicit promotion scope; defaults to input paths')
    parser.add_argument('--starter', action='store_true', help='Create a small starter file only when applying')
    parser.add_argument('--create-project', action='store_true', help='Allow creation of the selected directory')
    parser.add_argument('--non-interactive', action='store_true', help='Print JSON preview without prompting')
    parser.add_argument('--apply', action='store_true', help='Apply a non-interactive plan explicitly')


def ask(prompt):
    return input(prompt).strip()


def print_preview(report):
    """Show choices that affect the user, leaving hashes to the JSON surface."""
    print('Project: ' + report['project'])
    if report['create_project']: print('The selected project directory will be created.')
    print('Files to share: ' + ', '.join(report['include']))
    print('Explicit promotion scope: ' + ', '.join(report['writable']))
    print('Selected project rules: ' + (', '.join(report['selected_rules']) or 'none'))
    for name, check in report['diagnostics']['commands'].items():
        print(name + ': ' + check['status'] + ' - ' + check.get('executable', check.get('next_action', '')))
    changes = [item for item in report['actions'] if item['action'] != 'keep']
    for item in changes: print(item['action'] + ': ' + item['path'])
    if not changes: print('Connection files are already up to date.')
    print('Authentication has not been checked. No AI request will be sent.')
    print(report['note'])


def choose_endpoint(provider, explicit, interactive):
    if explicit:
        command = command_path(explicit)
    else:
        found = discover(provider)
        usable = [c for c in found if c['status'] == 'FOUND']
        if interactive:
            for index, item in enumerate(usable, 1):
                print(f'{index}. {item["path"]}')
            for item in found:
                if item['status'] != 'FOUND':
                    print(item['path'] + ': ' + item['next_action'])
            if usable:
                selection = ask(f'Select {provider} number, or enter its full executable path: ')
                if selection.isdecimal() and 1 <= int(selection) <= len(usable):
                    command = usable[int(selection)-1]['command']
                else:
                    command = command_path(selection)
            else:
                selection = ask(f'{provider} not found. Enter its installed executable path (blank cancels): ')
                if not selection: raise EOFError
                command = command_path(selection)
        elif len(usable) != 1:
            raise ValueError(f'{provider}: expected one supported CLI; found {len(usable)}. Install the official CLI or specify --{provider} PATH (native executable or JS entry point with Node)')
        else:
            command = usable[0]['command']
    endpoint = dict(adapter=provider, command=list(command), parallel=1)
    if provider == 'codex':
        endpoint['sandbox'] = 'read-only'
    else:
        endpoint['command'] += ['--tools', 'Read']
    return endpoint


def execute(args):
    interactive = not args.non_interactive
    try:
        if interactive and not sys.stdin.isatty():
            raise ValueError('Use a terminal for the wizard, or --non-interactive with explicit selections')
        project = args.project
        if project is None:
            if not interactive: raise ValueError('--project is required')
            selected = ask('Project directory (blank cancels): ')
            if not selected: raise EOFError
            project = Path(selected)
        project = connect.project_path(project)
        create = args.create_project
        if not project.exists() and not create and interactive:
            create = ask('Create this project directory when applying? [y/N] ').lower() == 'y'
            if not create: raise EOFError
        existing = (project / 'bridge.json').exists()
        include, writable, starter = args.include, args.writable, args.starter
        endpoints = None
        providers = args.provider or [p for p in PROVIDERS if getattr(args, p)]
        if any(getattr(args, p) and p not in providers for p in PROVIDERS):
            raise ValueError('Each explicit CLI path must belong to a selected provider')
        if existing and (providers or include is not None or writable is not None or starter):
            raise ValueError('Existing bridge.json is preserved; omit new configuration options')
        if not existing:
            if not providers and interactive:
                providers = ask('Providers (codex, claude; comma-separated): ').replace(' ', '').split(',')
            if not providers or any(p not in PROVIDERS for p in providers):
                raise ValueError('Select --provider codex and/or --provider claude')
            endpoints = {p: choose_endpoint(p, getattr(args, p), interactive) for p in dict.fromkeys(providers)}
            if not include and not starter and interactive:
                print('Select only source files to send. Credentials and integration directories are not eligible.')
                if project.is_dir():
                    choices = [p.name for p in sorted(project.iterdir())
                               if not p.name.startswith('.') and p.name not in INTEGRATION
                               and p.name not in workspace.EXCLUDED and not sensitive(p.name)
                               and not p.is_symlink() and not (hasattr(p, 'is_junction') and p.is_junction())]
                    print('Top-level choices: ' + (', '.join(choices[:20]) or 'none'))
                    if len(choices) > 20: print('More paths are available; enter their project-relative names.')
                selected = ask('Input paths (comma-separated, or blank for a starter file): ')
                if selected:
                    include = [p.strip() for p in selected.split(',')]
                else:
                    starter = ask('Create PATCHPORT_START.md when applying? [y/N] ').lower() == 'y'
                    if not starter: raise EOFError
            if interactive and writable is None:
                selected = ask('Promotion paths within input scope (comma-separated; blank uses input scope): ')
                if selected: writable = [p.strip() for p in selected.split(',')]
        plan = plan_setup(project, python=args.python, endpoints=endpoints, include=include,
                          writable=writable, starter=starter, create_project=create)
        if interactive:
            print_preview(plan['report'])
            if not plan['report']['ok']: return 2
            if ask('Apply these local files? No login, AI request, or runner start. [y/N] ').lower() != 'y':
                print(json.dumps(dict(ok=True, connection='CANCELLED', ai_calls=0, runner_started=False)))
                return 0
            result = apply_setup(plan)
        else:
            result = apply_setup(plan) if args.apply else plan['report']
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 2
    except (EOFError, KeyboardInterrupt):
        print(json.dumps(dict(ok=True, connection='CANCELLED', ai_calls=0, runner_started=False)))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(ok=False, connection='ERROR', error=str(exc), ai_calls=0,
                              runner_started=False), ensure_ascii=False))
        return 1
