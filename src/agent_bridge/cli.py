import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import time
from .storage import load_config, Store, atomic_json, configure_output
from .runner import run, argv_for
from . import workspace
from .cleanup import prune
from .usage import report as usage_report
from . import context
from .health import inspect as runner_status
from .answers import concise


def main(argv=None):
    configure_output()
    parser = argparse.ArgumentParser(prog="agent-bridge")
    parser.add_argument("--config", default="bridge.json")
    sub = parser.add_subparsers(dest="action",required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    from .connect import add_arguments, execute as connect_project
    connection = sub.add_parser("connect", help="Preview or install local project integration; no login or AI calls")
    add_arguments(connection)
    submit = sub.add_parser("submit")
    submit.add_argument("--id", required=True)
    submit.add_argument("--targets", nargs="+", required=True)
    submit.add_argument("--prompt-file", required=True)
    submit.add_argument("--context-plan", help="Freeze a full/omit plan or a reviewed summary plan")
    preview = sub.add_parser("context", help="Preview selected input and budgets without submitting or calling a provider")
    preview.add_argument("--prompt-file", required=True)
    preview.add_argument("--context-plan", help="Preview full/omit or reviewed summary selection without submitting")
    worker = sub.add_parser("run"); worker.add_argument("--once",action="store_true")
    sub.add_parser("status")
    usage = sub.add_parser("usage", help="Report recorded usage; missing values remain unknown")
    usage.add_argument("--days", type=int)
    usage.add_argument("--endpoint")
    usage.add_argument("--batch")
    usage.add_argument("--group-by", choices=["endpoint", "model", "batch", "day"], default="endpoint")
    cleanup = sub.add_parser("prune", help="Preview old terminal task artifacts; runner must be stopped")
    cleanup.add_argument("--days", type=int, default=30)
    cleanup.add_argument("--ids", nargs="+")
    cleanup.add_argument("--apply", action="store_true", help="Delete artifacts for explicitly selected eligible task IDs")
    sub.add_parser("pause", help="Stop new claims; already claimed work continues")
    sub.add_parser("resume", help="Allow new claims without resetting the call limit")
    limit = sub.add_parser("limit", help="Set the cumulative claim limit for this state directory")
    limit_args = limit.add_mutually_exclusive_group(required=True)
    limit_args.add_argument("--max-calls", type=int)
    limit_args.add_argument("--unlimited", action="store_true")
    result = sub.add_parser("result"); result.add_argument("--id",required=True)
    wait = sub.add_parser("wait"); wait.add_argument("--id",required=True); wait.add_argument("--timeout",type=float,default=600)
    result.add_argument("--brief", action="store_true", help="Show answer, outcome and changed paths without detailed plans")
    wait.add_argument("--brief", action="store_true", help="Show answer, outcome and changed paths without detailed plans")
    review = sub.add_parser("review"); review.add_argument("--task",required=True)
    apply = sub.add_parser("apply"); apply.add_argument("--task",required=True); apply.add_argument("--paths",nargs="+",required=True)
    recover = sub.add_parser("recover"); recover.add_argument("--task",required=True); recover.add_argument("--backup",required=True); recover.add_argument("--apply",action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "connect":
            if args.config != "bridge.json":
                raise ValueError("connect uses --project and --config-template, not --config")
            return connect_project(args)
        if args.action == "init":
            path=Path(args.config)
            if path.exists(): raise ValueError("Config exists; refusing overwrite")
            atomic_json(path, dict(project=".",state=".agent-bridge",include=["README.md","src"],writable=["src"],parallel=2,timeout=600,endpoints={"claude":dict(adapter="claude",command=["claude"],model="haiku",parallel=1),"codex":dict(adapter="codex",command=["codex"],sandbox="read-only",parallel=1)}))
            print("Created config. Review include/writable and endpoint commands before submitting.")
            return 0
        c=load_config(args.config)
        if args.action == "doctor":
            output={"python":sys.version.split()[0],"project":str(c["root"]),
                    "endpoints":{},"checks":{},"ok":True,
                    "scope":"Command resolution only; authentication, sandbox startup and task success are not tested."}
            for name, endpoint in c["endpoints"].items():
                try:
                    argv = argv_for(endpoint, c["state"] / "doctor-reply.txt")
                    output["endpoints"][name] = argv[0]
                    output["checks"][name] = {"ok":True}
                except (OSError, ValueError) as exc:
                    output["endpoints"][name] = None
                    output["checks"][name] = {"ok":False,"error":str(exc)}
                    output["ok"] = False
            print(json.dumps(output,ensure_ascii=False,indent=2))
            return 0 if output["ok"] else 1
        if args.action in ("context", "submit"):
            prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig")
            inventory = (context.plan_preview(c, prompt, args.context_plan)
                         if args.context_plan else context.preview(c, prompt))
            if args.action == "context" or not inventory["ok"]:
                print(json.dumps(inventory, ensure_ascii=False, indent=2))
                return 0 if inventory["ok"] else 2
            saved_plan = None
            if args.context_plan:
                if any(name not in c["endpoints"] for name in args.targets):
                    raise ValueError("Unknown endpoint")
                saved_plan = context.freeze_plan(c, prompt, inventory, args.targets)
        store=Store(c["state"])
        if args.action == "submit":
            output={"tasks":store.submit(args.id,args.targets,prompt,c["endpoints"], saved_plan), "context":inventory}
            if saved_plan:
                output["context"].update(scope="submitted_plan", plan_sha256=saved_plan["plan_sha256"], selection_applied_to_execution=True)
        elif args.action == "run": return run(c,args.once) or 0
        elif args.action == "usage": output = usage_report(store,args.days,args.endpoint,args.batch,args.group_by)
        elif args.action == "prune":
            output = prune(c, args.days, args.ids, args.apply)
            print(json.dumps(output,ensure_ascii=False,indent=2))
            return 2 if args.apply and any(not item["deleted"] for item in output["tasks"]) else 0
        elif args.action in ("pause", "resume"):
            store.pause(args.action == "pause")
            output = store.control()
        elif args.action == "limit":
            store.set_limit(None if args.unlimited else args.max_calls)
            output = store.control()
        elif args.action == "status":
            output=runner_status(c["state"])
            output.update(counts=dict(Counter(r["state"] for r in store.rows())), control=store.control())
        elif args.action in ("result","wait"):
            deadline=time.monotonic()+getattr(args,"timeout",0)
            while True:
                output=store.rows(args.id)
                if not output: raise ValueError("Unknown batch")
                if args.action != "wait" or not any(r["state"] in ("QUEUED","RUNNING") for r in output): break
                if time.monotonic() >= deadline: raise TimeoutError("Wait timed out; tasks remain queued/running")
                time.sleep(.2)
            for r in output:
                r.pop("prompt",None)
                if r["result"]: r["result"]=json.loads(r["result"])
                r["task_success"] = "NOT_EVALUATED"
                r["artifact_cleanup"] = store.artifact_status(r["id"])
                if not args.brief:
                    try: r["context_plan"] = store.context_plan(r["id"])
                    except ValueError as exc: r["context_plan"] = {"error": "Invalid stored context plan: " + str(exc)}
            if args.brief: output = [concise(r) for r in output]
            print(json.dumps(output,ensure_ascii=False,indent=2))
            return 0 if all(r["state"] == "DONE" for r in output) else 2
        elif args.action == "review": output=workspace.review(c,args.task)
        elif args.action == "apply": output=workspace.review(c,args.task,args.paths)
        elif args.action == "recover": output=workspace.recover(c,args.task,args.backup,args.apply)
        print(json.dumps(output,ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,RuntimeError,TimeoutError,sqlite3.Error) as exc:
        print(type(exc).__name__+": "+str(exc),file=sys.stderr)
        return 1

if __name__ == "__main__": raise SystemExit(main())
