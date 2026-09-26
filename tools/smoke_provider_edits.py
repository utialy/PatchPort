"""Exercise real provider edits and CLI review/apply/recover in a disposable fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", required=True)
    parser.add_argument("--codex-command", nargs="+", required=True)
    parser.add_argument("--targets", nargs="+", choices=["claude", "codex"], default=["claude", "codex"])
    parser.add_argument("--inspect-run", type=Path, help="Inspect an existing run without provider calls")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    parent = root / ".bridge" / "provider-edit-smoke"
    run = args.inspect_run.resolve() if args.inspect_run else parent / uuid.uuid4().hex
    if run.parent != parent.resolve():
        parser.error("Run must be directly inside .bridge/provider-edit-smoke")
    run.mkdir(parents=True, exist_ok=bool(args.inspect_run))
    before, after = b"BRIDGE_BEFORE\n", b"BRIDGE_AFTER\n"
    fixture = run / "fixture.txt"
    path = run / "bridge.json"
    if not args.inspect_run:
        fixture.write_bytes(before)
        (run / "request.md").write_text(
            "Edit only fixture.txt in the current working copy: replace BRIDGE_BEFORE with "
            "BRIDGE_AFTER, preserving one trailing newline. Read it first and make the edit. "
            "Do not modify other files, install anything, or launch agents. "
            "Return exactly BRIDGE_EDIT_OK after the edit succeeds.", encoding="utf-8")
        config = dict(project=".", state=".agent-bridge", include=["fixture.txt"],
                      writable=["fixture.txt"], parallel=2, timeout=120, endpoints={
                          "claude": dict(adapter="claude", model="haiku",
                                         command=[args.claude, "--tools", "Read,Edit",
                                                  "--allowedTools", "Read(./fixture.txt)", "Edit(./fixture.txt)"]),
                          "codex": dict(adapter="codex", command=args.codex_command,
                                        sandbox="workspace-write")})
        path = run / "bridge.json"
        config["endpoints"] = {name: config["endpoints"][name] for name in args.targets}
        config["smoke_batch"] = "edit-" + uuid.uuid4().hex[:12]
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    saved_config = json.loads(path.read_text(encoding="utf-8"))
    targets = list(saved_config["endpoints"])
    batch = saved_config.get("smoke_batch", "edit")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    base = [sys.executable, "-m", "agent_bridge", "--config", str(path)]

    def cli(label, *command, expected=0):
        result = subprocess.run(base + list(command), env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=160)
        (run / (label + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode != expected:
            raise RuntimeError(f"{label}: exit {result.returncode}, expected {expected}")
        return json.loads(result.stdout) if command[0] != "run" and result.stdout.strip() else None

    report = dict(artifacts=str(run), providers=[])
    try:
        if not args.inspect_run:
            cli("doctor", "doctor")
            cli("submit", "submit", "--id", batch, "--targets", *targets,
                "--prompt-file", str(run / "request.md"))
            cli("run", "run", "--once")
        # result returns 2 for provider failures; preserve those details for the report.
        rows_result = subprocess.run(base + ["result", "--id", batch], env=env,
                                     capture_output=True, text=True, encoding="utf-8", timeout=20)
        (run / "result.log").write_text(rows_result.stdout + rows_result.stderr, encoding="utf-8")
        rows = json.loads(rows_result.stdout)
        if sorted(row["endpoint"] for row in rows) != sorted(targets):
            raise RuntimeError("Provider results do not match configured targets")
        for row in rows:
            item = dict(endpoint=row["endpoint"], state=row["state"], passed=False)
            report["providers"].append(item)
            try:
                task = row["id"]
                home = run / ".agent-bridge" / "workspaces" / task
                answer = (home / "answer.txt").read_text(encoding="utf-8")
                item["answer"] = answer
                if row["state"] != "DONE" or answer.strip() != "BRIDGE_EDIT_OK":
                    raise RuntimeError("Provider did not report successful edit")
                proposal = (home / "project" / "fixture.txt").read_bytes()
                if proposal not in (after, after.replace(b"\n", b"\r\n")):
                    raise RuntimeError("Unexpected proposed content")
                manifest = cli(task + "-review", "review", "--task", task)
                expected = [dict(path="fixture.txt", before=hashlib.sha256(before).hexdigest(),
                                 after=hashlib.sha256(proposal).hexdigest(), allowed=True)]
                if manifest != expected or fixture.read_bytes() != before:
                    raise RuntimeError("Unexpected manifest or original changed before apply")
                applied = cli(task + "-apply", "apply", "--task", task, "--paths", "fixture.txt")
                if applied["state"] != "APPLIED" or fixture.read_bytes() != proposal:
                    raise RuntimeError("Apply did not install exact proposal")
                backup = applied["backup"]
                preview = cli(task + "-preview", "recover", "--task", task, "--backup", backup)
                if preview["state"] != "APPLIED" or fixture.read_bytes() != proposal:
                    raise RuntimeError("Recovery preview mutated fixture")
                recovered = cli(task + "-recover", "recover", "--task", task, "--backup", backup, "--apply")
                if recovered["state"] != "ROLLED_BACK" or fixture.read_bytes() != before:
                    raise RuntimeError("Recovery failed to restore exact original")
                item.update(passed=True, backup=backup, review=True, apply=True,
                            preview_unchanged=True, restored=True)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                item["error"] = str(exc)
            if fixture.read_bytes() != before:
                raise RuntimeError("Fixture was not restored; stop before another proposal")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report["error"] = str(exc)
    report["original_intact"] = fixture.read_bytes() == before
    report["passed"] = (not report.get("error") and report["original_intact"]
                        and len(report["providers"]) == len(targets)
                        and all(item["passed"] for item in report["providers"]))
    (run / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
