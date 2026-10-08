"""Consume one durable role launch receipt; never retry an accepted workflow."""
import argparse
import json
import os
import time

from . import p1, runner_manager, workspace
from .storage import FileLock, atomic_json, identifier


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--nonce', required=True)
    args = parser.parse_args(argv)
    config = runner_manager.checked_config(args.config)
    flow_id = identifier(args.id)
    root = workspace.safe(config['state'], 'p1-jobs')
    receipt = workspace.safe(root, flow_id + '.json')
    with FileLock(root / 'start.lock'):
        ticket = json.loads(receipt.read_text(encoding='utf-8'))
        if ticket.get('state') != 'ACCEPTED' or ticket.get('launch_nonce') != args.nonce or ticket.get('config') != str(config['config_path']):
            raise ValueError('Role launch receipt identity mismatch; do not restart this ID')
        ticket.update(state='RUNNING', started=time.time(), job_pid=os.getpid())
        atomic_json(receipt, ticket)
    try:
        prompt = workspace.safe(root, flow_id + '.txt').read_text(encoding='utf-8')
        if p1.role_start_report(config, dict(flow_id=flow_id, prompt=prompt)) != ticket['expected']:
            raise ValueError('Role input changed after launch preview')
        result = p1.helper('role_flow').develop_review(config['root'], prompt, flow_id)
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
