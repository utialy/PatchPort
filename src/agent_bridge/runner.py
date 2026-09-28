"""Bounded multi-endpoint execution. No shell interpolation or automatic retries."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from .storage import Store, FileLock, atomic_json, configure_output, load_config, identifier
from . import workspace
from .usage import snapshot
from . import context
from .health import code_identity
from .answers import capture

PRINT_LOCK = threading.Lock()

def argv_for(endpoint, reply):
    argv = list(endpoint["command"])
    executable = shutil.which(argv[0])
    if not executable: raise ValueError("CLI executable not found: " + argv[0])
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("Use a native .exe or [node, absolute/path/to/cli.js] on Windows; shell shims are not executed")
    # The worker changes cwd to its copy: never retain a relative executable path.
    argv[0] = str(Path(executable).resolve())
    adapter = endpoint["adapter"]
    if adapter == "claude":
        argv += ["-p", "--output-format", "stream-json", "--verbose"]
    elif adapter == "codex":
        argv += ["exec", "--json", "--skip-git-repo-check", "--sandbox", endpoint.get("sandbox", "read-only"), "--output-last-message", str(reply), "-"]
    if adapter in ("claude", "codex") and endpoint.get("model"):
        # Codex options must precede the stdin prompt marker.
        if adapter == "codex": argv[-1:-1] = ["--model", endpoint["model"]]
        else: argv += ["--model", endpoint["model"]]
    return argv

def execute(config, row, stop_event, store=None):
    store = store if store is not None else Store(config["state"])
    id_ = row["id"]
    result = {"id": id_, "endpoint": row["endpoint"]}
    proc = None
    home = None
    try:
        endpoint = config["endpoints"][row["endpoint"]]
        store.record_usage(id_, snapshot(endpoint["adapter"], requested_model=endpoint.get("model")))
        if row.get("context_plan_required"):
            result["context"] = dict(scope="execution_plan", validation="FAILED")
        saved = store.context_plan(id_)
        if row.get("context_plan_required"):
            result["context"] = dict(scope="execution_plan", validation="FAILED", stored_plan=saved)
            result["context"] = context.check_plan(config, row["prompt"], saved, row["endpoint"])
        else:
            if saved is not None:
                raise ValueError("Context plan task state is inconsistent")
            result["context"] = context.preview(config, row["prompt"])
        context.enforce(result["context"])
        if saved is not None:
            home = workspace.create(config, id_, [i["path"] for i in saved["payload"]["selected"]])
            result["context"]["validation"] = "COPY_PENDING"
            inventory = context.check_plan(config, row["prompt"], saved, row["endpoint"], copied=home / "project")
            context.check_plan(config, row["prompt"], saved, row["endpoint"])
        else:
            home = workspace.create(config, id_)
            inventory = context.preview(config, row["prompt"], copied=home / "project")
        result["context"] = inventory
        atomic_json(home / "context.json", inventory)
        context.enforce(inventory)
        prompt = context.render_prompt(row["prompt"], saved)
        prompt_file = home / "prompt.txt"
        # Preserve the bytes counted by the input budget on every platform.
        prompt_file.write_bytes(prompt.encode("utf-8"))
        reply = home / "provider-reply.txt"
        spec = dict(argv=argv_for(endpoint, reply), cwd=str(home / "project"), prompt_file=str(prompt_file), exit_file=str(home / "exit-code.txt"))
        atomic_json(home / "process.json", spec)
        env = dict(os.environ)
        # Bootstrap remains importable after cwd changes, including from an uninstalled checkout.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen([sys.executable, "-m", "agent_bridge.child", str(home / "process.json")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=flags)
        proc.stdin.write(b"GO\n"); proc.stdin.flush()
        messages = queue.Queue(maxsize=256)
        def read(stream, label):
            try:
                for line in iter(stream.readline, b""):
                    messages.put((label, line.decode("utf-8", errors="replace")))
            finally: messages.put((label, None))
        threads = [threading.Thread(target=read, args=(proc.stdout,"stdout"),daemon=True), threading.Thread(target=read,args=(proc.stderr,"stderr"),daemon=True)]
        for thread in threads: thread.start()
        ended = set(); terminal = None; agent_message = ""; failed = False; started = time.monotonic(); timed_out = False
        with (home / "events.jsonl").open("w",encoding="utf-8") as raw, (home / "stderr.log").open("w",encoding="utf-8") as err, (home / "answer.txt").open("w",encoding="utf-8") as answer:
            while len(ended) < 2:
                if stop_event.is_set() or time.monotonic() - started > config.get("timeout",600):
                    timed_out = True
                    proc.stdin.close()
                    proc.wait(timeout=10)
                try: label, text = messages.get(timeout=.1)
                except queue.Empty: continue
                if text is None: ended.add(label); continue
                (raw if label == "stdout" else err).write(text)
                (raw if label == "stdout" else err).flush()
                if label == "stdout":
                    if endpoint["adapter"] == "command": answer.write(text); answer.flush()
                    else:
                        try: event = json.loads(text)
                        except json.JSONDecodeError: event = {}
                        if not isinstance(event, dict): event = {}
                        if ((endpoint["adapter"] == "claude" and event.get("type") == "result" and event.get("subtype") != "error_during_execution") or
                                (endpoint["adapter"] == "codex" and event.get("type") in ("turn.completed", "turn.failed") and isinstance(event.get("usage"), dict))):
                            store.record_usage(id_, snapshot(endpoint["adapter"], event, endpoint.get("model")))
                        if endpoint["adapter"] == "claude" and event.get("type") == "result":
                            terminal = event; failed = bool(event.get("is_error")); answer.write(event.get("result", ""))
                        if endpoint["adapter"] == "codex":
                            if event.get("type") == "turn.completed": terminal = event
                            if event.get("type") == "item.completed" and event.get("item",{}).get("type") == "agent_message": agent_message = event["item"].get("text", "")
                            if event.get("type") in ("turn.failed", "error"): failed = True
                with PRINT_LOCK:
                    try: print(f"[{id_}][{label}] {text.rstrip()[:500]}",flush=True)
                    except (OSError, UnicodeError): pass  # Progress output must not abort provider work.
            code = proc.wait(timeout=10)
        exit_file = home / "exit-code.txt"
        if os.name != "nt" and exit_file.exists(): code = int(exit_file.read_text())
        if endpoint["adapter"] == "codex":
            if reply.exists(): shutil.copyfile(reply, home / "answer.txt")
            elif agent_message: (home / "answer.txt").write_text(agent_message,encoding="utf-8")
            else: failed = True
        if timed_out: status = "CANCELLED" if stop_event.is_set() else "TIMEOUT"
        elif code or failed or (endpoint["adapter"] != "command" and terminal is None): status = "ERROR"
        else: status = "DONE"
        result.update(exit_code=code, provider_result=terminal)
    except Exception as exc:
        status = "ERROR"
        result["error"] = type(exc).__name__ + ": " + str(exc)
        if row.get("context_plan_required") and "context" in result:
            result["context"].update(validation="FAILED", error=result["error"])
    finally:
        if proc:
            if proc.stdin and not proc.stdin.closed: proc.stdin.close()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            for stream in (proc.stdout,proc.stderr):
                if stream: stream.close()
        if home:
            try: result["changes"] = workspace.changes(config, home)
            except Exception as exc: status="ERROR"; result["manifest_error"] = str(exc)
            result["workspace"] = str(home)
            try: result["answer"] = capture(home, status)
            except (OSError, ValueError) as exc:
                result["answer"] = {"text": None, "status": "UNAVAILABLE", "error": str(exc)}
        store.finish(id_, status, result)
    return status

def role_config(parent, row, store):
    metadata = store.role_task(row['id'])
    if not isinstance(metadata, dict) or metadata.get('schema') != 1:
        raise ValueError('Missing or unsupported role execution metadata')
    flow = workspace.safe(parent['root'], '.role-flows/' + identifier(metadata['flow']))
    role = metadata['role']
    if role not in ('developer', 'reviewer') or row['endpoint'] != role:
        raise ValueError('Role identity mismatch')
    path = workspace.safe(flow, role + '-config.json')
    selected = load_config(path, data=metadata['config'])
    expected_root = parent['root'] if role == 'developer' else workspace.safe(flow, 'review-input')
    expected_state = workspace.safe(flow, 'developer-state' if role == 'developer' else 'review-input/.agent-bridge')
    if selected['root'] != expected_root or selected['state'] != expected_state or set(selected['endpoints']) != {role}:
        raise ValueError('Role execution paths or endpoints mismatch')
    return selected


def run(config, once=False, *, task_ids=None):
    configure_output()
    store = Store(config["state"])
    stop = threading.Event()
    with FileLock(config["state"] / "runner.lock"):
        # Capture once: later source edits must not refresh the running identity.
        identity = code_identity()
        started = time.time()
        store.recover()
        with ThreadPoolExecutor(max_workers=config.get("parallel",2)) as pool:
            active = {}
            last_beat = 0
            try:
                while True:
                    for future in list(active):
                        if future.done(): future.result(); del active[future]
                    counts = Counter(active.values())
                    for row in store.rows():
                        if row["state"] != "QUEUED": continue
                        if task_ids is not None and row['id'] not in task_ids: continue
                        if len(active) >= config.get("parallel",2): break
                        ep = row["endpoint"]
                        selected = config
                        if row.get('role_task_required'):
                            try:
                                selected = role_config(config, row, store)
                            except Exception as exc:
                                if store.claim(row['id']):
                                    store.finish(row['id'], 'ERROR', dict(error='Invalid role metadata: ' + str(exc)))
                                continue
                        if ep not in selected["endpoints"]:
                            store.finish(row["id"],"ERROR",{"error":"Endpoint removed from config"}); continue
                        if counts[ep] >= selected["endpoints"][ep].get("parallel",1): continue
                        if store.claim(row["id"]):
                            active[pool.submit(execute,selected,row,stop,store)] = ep; counts[ep] += 1
                    if time.monotonic()-last_beat > 2:
                        last_beat=time.monotonic()
                        try: atomic_json(config["state"] / "health.json", dict(schema=1, pid=os.getpid(), started=started, code=identity, heartbeat=time.time(),active=len(active)))
                        except OSError: pass  # Monitoring must not abort provider work.
                    if once and not active:
                        if not any(r["state"] == "QUEUED" and (task_ids is None or r['id'] in task_ids) for r in store.rows()): return 0
                        if store.control()["dispatch"] != "READY": return 2
                    time.sleep(.1)
            except KeyboardInterrupt:
                stop.set()
                for future in active: future.result()
