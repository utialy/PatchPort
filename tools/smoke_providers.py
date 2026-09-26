"""Run one explicit, isolated smoke invocation per configured provider.

Usage: python tools/smoke_providers.py --claude C:/path/claude.exe
       --codex-command C:/path/node.exe C:/path/codex.js
Artifacts stay under .bridge/provider-smoke/<unique-run>/.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude', required=True)
    parser.add_argument('--codex-command', nargs='+', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    run = root / '.bridge' / 'provider-smoke' / uuid.uuid4().hex
    run.mkdir(parents=True)
    (run / 'fixture.txt').write_text('Agent Bridge smoke fixture.\n', encoding='utf-8')
    (run / 'request.md').write_text(
        'Reply with exactly BRIDGE_SMOKE_OK. Do not use tools, read files, '
        'change files, or launch other agents. This only tests final response transport.',
        encoding='utf-8')
    config = dict(project='.', state='.agent-bridge', include=['fixture.txt'],
                  writable=['fixture.txt'], parallel=2, timeout=120,
                  endpoints={
                      'claude': dict(adapter='claude', command=[args.claude], model='haiku'),
                      'codex': dict(adapter='codex', command=args.codex_command, sandbox='read-only')})
    config_path = run / 'bridge.json'
    config_path.write_text(json.dumps(config, indent=2), encoding='utf-8')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(root / 'src') + os.pathsep + env.get('PYTHONPATH', '')
    base = [sys.executable, '-m', 'agent_bridge', '--config', str(config_path)]
    for name, command in [('doctor', ['doctor']),
                          ('submit', ['submit', '--id', 'smoke', '--targets', 'claude', 'codex',
                                      '--prompt-file', str(run / 'request.md')]),
                          ('run', ['run', '--once']),
                          ('result', ['result', '--id', 'smoke'])]:
        with (run / (name + '.log')).open('w', encoding='utf-8') as log:
            completed = subprocess.run(base + command, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, timeout=160)
        if completed.returncode and name != 'result':
            print(f'{name} failed; inspect {run}')
            return 1
    rows = json.loads((run / 'result.log').read_text(encoding='utf-8'))
    summary = []
    for row in rows:
        home = run / '.agent-bridge' / 'workspaces' / row['id']
        answer = (home / 'answer.txt').read_text(encoding='utf-8') if (home / 'answer.txt').exists() else ''
        result = row.get('result') or {}
        summary.append(dict(endpoint=row['endpoint'], state=row['state'],
                            answer_matches=answer.strip() == 'BRIDGE_SMOKE_OK',
                            changes=result.get('changes'), error=result.get('error')))
    original_intact = (run / 'fixture.txt').read_text(encoding='utf-8') == 'Agent Bridge smoke fixture.\n'
    report = dict(artifacts=str(run), original_intact=original_intact, providers=summary)
    (run / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if original_intact and all(r['state'] == 'DONE' and r['answer_matches'] and r['changes'] == [] for r in summary) else 1


if __name__ == '__main__':
    raise SystemExit(main())
