"""Preview or install a project-scoped Bridge launcher and peer-consult skills."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from agent_bridge import context, workspace
from agent_bridge.storage import load_config, atomic_json

def asset_root():
    """Use wheel assets or the explicit source checkout layout, never the cwd."""
    packaged = Path(__file__).parent / "_connect_assets"
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "src/agent_bridge/connect.py").resolve() == Path(__file__).resolve() and (checkout / "tools/templates/project_launcher.py.in").is_file():
        return checkout
    raise ValueError("Connection assets are missing; reinstall the package")
MANIFEST = '.bridge-integration.json'


def digest(data): return hashlib.sha256(data).hexdigest()


def config_input(project, relative, template):
    path = workspace.safe(project, relative)
    if path.exists():
        return path.read_bytes(), False
    if template is None:
        raise ValueError(relative + ' is missing; supply its explicit template')
    value = json.loads(Path(template).read_text(encoding='utf-8-sig'))
    if relative == 'bridge.json':
        value['project'] = '.'
        if Path(value.get('state', '.agent-bridge')).is_absolute():
            raise ValueError('Config template state must be project-relative')
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode(), True


def plan_install(project, python=None, config_template=None, roles_template=None, adopt=False):
    ROOT = asset_root()
    project = Path(project).resolve()
    if not project.is_dir(): raise ValueError('Create the intended project directory first')
    if (project.parent/'baseline.json').exists() or '.role-flows' in project.parts:
        raise ValueError('Do not install integrations in delegated task copies')
    python = Path(python or sys.executable).resolve()
    if not python.is_file(): raise ValueError('Python executable not found')
    config_bytes, create_config = config_input(project, 'bridge.json', config_template)
    raw = json.loads(config_bytes.decode('utf-8-sig'))
    # Validate with the real project root, without creating queue/state in that project.
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp)/'bridge.json'
        check = dict(raw, project=str((project/raw.get('project','.')).resolve()))
        path.write_text(json.dumps(check),encoding='utf-8')
        config = load_config(path)
        if config['root'] != project: raise ValueError('bridge.json must refer to the selected project root')
        if project.is_relative_to(config['state']/'workspaces') or '.role-flows' in project.parts:
            raise ValueError('Do not install integrations in delegated task copies')
        inventory = context.preview(config)
        if inventory['missing']: raise ValueError('Config contains missing include paths')
    contents = {}
    if create_config: contents['bridge.json'] = config_bytes
    role_path = workspace.safe(project, 'role_workflow.json')
    if role_path.exists() or roles_template is not None:
        role_bytes, create_role = config_input(project, 'role_workflow.json', roles_template)
        policy = json.loads(role_bytes.decode('utf-8-sig'))
        from .role_policy import readonly
        if not isinstance(policy,dict) or set(policy) != {'developer','reviewer','checks'}:
            raise ValueError('Invalid role policy fields')
        for role in ('developer','reviewer'):
            endpoint=policy[role]
            if not isinstance(endpoint,dict) or not isinstance(endpoint.get('command'),list) or not endpoint['command'] or not all(isinstance(a,str) and a for a in endpoint['command']):
                raise ValueError('Role commands must be nonempty string arrays')
        if not readonly(policy['reviewer']):
            raise ValueError('Role policy requires developer/reviewer/checks and a read-only reviewer')
        if not isinstance(policy['checks'],list) or not policy['checks'] or any(not isinstance(c,list) or not c or not all(isinstance(a,str) and a for a in c) for c in policy['checks']):
            raise ValueError('Role policy requires explicit checks')
        if create_role: contents['role_workflow.json'] = role_bytes
    launcher = (ROOT/'tools/templates/project_launcher.py.in').read_text(encoding='utf-8')
    rendered = launcher.replace('__BRIDGE_PYTHON__', repr(str(python)))
    compile(rendered, 'bridge.py', 'exec')
    contents['bridge.py'] = rendered.encode()
    contents['role_flow.py'] = (ROOT/'tools/role_flow.py').read_bytes()
    contents['project_overview.py'] = (ROOT/'tools/project_overview.py').read_bytes()
    contents['role_review.py'] = (ROOT/'tools/role_review.py').read_bytes()
    contents['role_manage.py'] = (ROOT/'tools/role_manage.py').read_bytes()
    contents['flow_archive.py'] = (ROOT/'tools/flow_archive.py').read_bytes()
    contents['flow_lifecycle.py'] = (ROOT/'tools/flow_lifecycle.py').read_bytes()
    contents['flow_cleanup.py'] = (ROOT/'tools/flow_cleanup.py').read_bytes()
    contents['flow_archive_writer.py'] = (ROOT/'tools/flow_archive_writer.py').read_bytes()
    for location in ('.agents/skills/peer-consult', '.claude/skills/peer-consult'):
        for relative in ('SKILL.md','references/usage.md','references/roles.md'):
            contents[location+'/'+relative] = (ROOT/'skills/peer-consult'/relative).read_bytes()
    request_path=workspace.safe(project,'tasks/request.md')
    if not request_path.exists():
        contents['tasks/request.md']=b'Review the selected project files without editing them. Report findings and limitations.\n'
    previous={}
    manifest_path=workspace.safe(project,MANIFEST)
    if manifest_path.exists():
        previous=json.loads(manifest_path.read_text(encoding='utf-8'))
        if previous.get('schema')!=1 or not isinstance(previous.get('files'),dict):
            raise ValueError('Invalid integration manifest; inspect before updating')
    managed={name for name in contents if name not in ('bridge.json','role_workflow.json','tasks/request.md')}
    actions=[]
    for name,data in contents.items():
        target=workspace.safe(project,name)
        old=target.read_bytes() if target.exists() else None
        state='create' if old is None else 'keep' if old==data else 'update'
        if state=='update' and previous.get('files',{}).get(name)!=digest(old) and not adopt:
            state='conflict'
        actions.append(dict(path=name,action=state,before=digest(old) if old is not None else None,after=digest(data)))
    return dict(project=project,python=python,contents=contents,managed=managed,actions=actions,
                roles=role_path.exists() or roles_template is not None,
                ok=not any(a['action']=='conflict' for a in actions))


def install(plan, apply=False):
    report=dict(project=str(plan['project']),python=str(plan['python']),apply=apply,ok=plan['ok'],roles_configured=plan['roles'],actions=plan['actions'],runner_started=False,ai_calls=0)
    if not apply or not plan['ok']: return report
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    checked=subprocess.run([str(plan['python']),'-c','import agent_bridge; from agent_bridge.storage import probe_lock, readonly_database, durable_write, sync_directory; from agent_bridge.role_policy import readonly; print(agent_bridge.__version__)'],env=env,capture_output=True,timeout=15)
    if checked.returncode:raise ValueError('Selected Python needs the current agent_bridge runtime with connection and role policy support; install the updated package first')
    project=plan['project']
    # Recheck all intended destinations before the first project write.
    for item in plan['actions']:
        target=workspace.safe(project,item['path'])
        current=digest(target.read_bytes()) if target.exists() else None
        if current!=item['before']:raise ValueError('Destination changed after preview: '+item['path'])
    desired_manifest=dict(schema=1,python=str(plan['python']),files={name:digest(plan['contents'][name]) for name in sorted(plan['managed'])})
    manifest=workspace.safe(project,MANIFEST)
    if all(a['action']=='keep' for a in plan['actions']) and manifest.exists() and json.loads(manifest.read_text(encoding='utf-8'))==desired_manifest:
        report['unchanged']=True
        return report
    backup=workspace.safe(project,'.bridge-integration-backups/'+uuid.uuid4().hex)
    backup.mkdir(parents=True)
    manifest=workspace.safe(project,MANIFEST)
    if manifest.exists():shutil.copy2(manifest,backup/'previous-manifest.json')
    for item in plan['actions']:
        if item['action']=='update':
            saved=workspace.safe(backup,item['path']);saved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(workspace.safe(project,item['path']),saved)
    atomic_json(backup/'plan.json',report)
    try:
        for item in plan['actions']:
            if item['action']=='keep':continue
            target=workspace.safe(project,item['path'])
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
            temporary.write_bytes(plan['contents'][item['path']])
            os.replace(temporary,target)
        atomic_json(manifest,desired_manifest)
    except Exception as exc:
        atomic_json(backup/'error.json',dict(error=str(exc),note='Installation may be partial; inspect backup before retry'))
        raise
    report['backup']=str(backup)
    return report


def add_arguments(parser):
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--python',type=Path)
    parser.add_argument('--config-template',type=Path)
    parser.add_argument('--roles-template',type=Path)
    parser.add_argument('--adopt-existing',action='store_true',help='Back up and replace only conflicting managed integration files')
    parser.add_argument('--apply',action='store_true')


def execute(args):
    try:
        plan=plan_install(args.project,args.python,args.config_template,args.roles_template,args.adopt_existing)
        result=install(plan,args.apply)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['ok'] else 2
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print(json.dumps(dict(ok=False,error=str(exc)),ensure_ascii=False))
        return 1

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    return execute(args)


if __name__ == '__main__':
    raise SystemExit(main())
