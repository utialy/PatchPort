"""Consume one durable role launch receipt; never retry an accepted workflow."""
import argparse
import contextlib
import json
import os
import time

from . import p1, runner_manager, workspace
from .storage import FileLock, atomic_json, identifier


@contextlib.contextmanager
def start_lock(root, timeout=15):
    """Wait for the parent's launch handoff, without relaunching any work."""
    lock=FileLock(root/'start.lock'); deadline=time.monotonic()+timeout
    while True:
        try:
            lock.__enter__(); break
        except RuntimeError as error:
            if str(error)!='Bridge lock is busy' or time.monotonic()>=deadline: raise
            time.sleep(.02)
    try: yield
    finally: lock.__exit__(None,None,None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--nonce', required=True)
    args = parser.parse_args(argv)
    config = runner_manager.checked_config(args.config)
    flow_id = identifier(args.id)
    from . import records_flow
    if records_flow.supported(config,live=True):
        receipt=workspace.safe(config['state'],'p1-jobs/'+flow_id+'.json')
        with start_lock(receipt.parent):
            ticket=json.loads(receipt.read_text(encoding='utf-8'))
            if ticket.get('state')!='ACCEPTED' or ticket.get('launch_nonce')!=args.nonce or ticket.get('config')!=str(config['config_path']):
                raise ValueError('Role launch receipt identity mismatch; do not restart this ID')
        lifetime=records_flow.Writer(config,flow_id,ticket.get('flow_owner'))
    else:
        lifetime=contextlib.nullcontext(None)
    with lifetime as writer:
        return execute(config,flow_id,args,writer)


def execute(config,flow_id,args,writer):
    root = workspace.safe(config['state'], 'p1-jobs')
    receipt = workspace.safe(root, flow_id + '.json')
    with start_lock(root):
        ticket = json.loads(receipt.read_text(encoding='utf-8'))
        if ticket.get('state') != 'ACCEPTED' or ticket.get('launch_nonce') != args.nonce or ticket.get('config') != str(config['config_path']):
            raise ValueError('Role launch receipt identity mismatch; do not restart this ID')
        ticket.update(state='RUNNING', started=time.time(), job_pid=os.getpid())
        atomic_json(receipt, ticket)
    try:
        prompt = workspace.safe(root, flow_id + '.txt').read_text(encoding='utf-8')
        if p1.role_start_report(config, dict(flow_id=flow_id, prompt=prompt),accepted=True) != ticket['expected']:
            raise ValueError('Role input changed after launch preview')
        result = p1.helper('role_flow').develop_review(config['root'], prompt, flow_id, flow_writer=writer)
        ticket.update(state='FINISHED', workflow_state=result['state'])
        code = 0 if result['state'] == 'REVIEW_DONE' else 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        ticket.update(state='FAILED', workflow_state='INSPECT_LOG_AND_STATE')
        code = 1
    ticket.update(finished=time.time(), exit_code=code)
    atomic_json(receipt, ticket)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
