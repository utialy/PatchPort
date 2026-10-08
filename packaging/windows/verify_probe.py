"""Exercise the assembled candidate away from the repository and system Python."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def verify(candidate, report_path):
    destination = Path(tempfile.mkdtemp(prefix='PatchPort packaging ')).resolve()
    bundle = destination / 'bundle'
    shutil.copytree(candidate, bundle)
    project = destination / '\ud55c\uae00 project'
    project.mkdir()
    core = bundle / 'core/python.exe'
    gui = bundle / 'gui/PatchPortProbe.exe'
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PYTHON', '_PYI')) and key != '_MEIPASS2'}
    env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    env['PYTHONUTF8'] = '1'
    calls = []
    def command(arguments, expected=0):
        begin = time.perf_counter()
        process = subprocess.run([str(x) for x in arguments], cwd=project, env=env,
                                 capture_output=True, text=True, encoding='utf-8',
                                 timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
        calls.append(dict(command=[str(x) for x in arguments], exit=process.returncode,
                          seconds=round(time.perf_counter() - begin, 3)))
        if process.returncode != expected:
            raise RuntimeError(dict(command=calls[-1], stdout=process.stdout, stderr=process.stderr))
        return process.stdout
    report = dict(destination=str(destination), platform='Windows x64',
                  clean_vm=False, sanitized_path=True, calls=calls, provider_calls=0)
    stage = 'core'
    try:
        runtime = json.loads(command([core, '-c',
            'import json,sys,sqlite3,agent_bridge; print(json.dumps(dict(executable=sys.executable,version=sys.version,isolated=sys.flags.isolated,paths=sys.path,package=agent_bridge.__file__,deserialize=hasattr(sqlite3.Connection,"deserialize"))))']))
        assert runtime['isolated'] and runtime['deserialize'], runtime
        assert all(Path(path).resolve().is_relative_to(bundle) for path in runtime['paths']), runtime
        report['runtime'] = runtime
        (project / 'input.txt').write_text('mock context', encoding='utf-8')
        config = dict(project='.', include=['input.txt'], writable=['input.txt'], parallel=1,
                      endpoints={'mock': dict(adapter='command', command=[str(core), '-c', 'print("packaged reply")'])})
        (project / 'bridge.json').write_text(json.dumps(config), encoding='utf-8')
        base = [core, '-m', 'agent_bridge']
        preview = json.loads(command(base + ['setup', '--project', project, '--non-interactive']))
        assert not (project / '.agent-bridge').exists()
        applied = json.loads(command(base + ['setup', '--project', project, '--non-interactive', '--apply']))
        assert applied['ok'], applied
        manifest = json.loads((project / '.bridge-integration.json').read_text())
        assert Path(manifest['python']) == core
        launcher = [core, project / 'bridge.py']
        runner = json.loads(command(launcher + ['runner', 'start', '--apply']))
        try:
            (project / 'request.txt').write_text('reply locally', encoding='utf-8')
            command(launcher + ['submit', '--id', 'packaging', '--targets', 'mock', '--prompt-file', project / 'request.txt'])
            answer = json.loads(command(launcher + ['wait', '--id', 'packaging', '--timeout', '15']))
            assert answer[0]['state'] == 'DONE', answer
            status = json.loads(command(launcher + ['runner', 'status']))
            assert status['control']['calls_started'] == 1, status
            assert Path(status['management']['identity']['python']) == core
            report['mock_result'] = 'DONE'
        finally:
            stopped = json.loads(command(launcher + ['runner', 'stop', '--apply', '--run-id', runner['run_id']]))
            assert stopped['outcome'] == 'STOPPED'
        gui_report = destination / 'gui-report.json'
        stage = 'gui'
        command([gui, '--report', gui_report])
        view = json.loads(gui_report.read_text())
        assert view['ok'] and view['gui_frozen'] and view['mapped'], view
        assert Path(view['core']['python']) == core, view
        report['gui'] = view
        report['ok'] = True
    except Exception as exc:
        report.update(ok=False, error=str(exc), failed_stage=stage,
                      winerror=getattr(exc, 'winerror', None))
    report['bundle_bytes'] = sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file())
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report.get(key) for key in ('ok', 'error', 'destination', 'bundle_bytes', 'mock_result')}, ensure_ascii=True))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(verify(args.candidate.resolve(), args.report.resolve()))
