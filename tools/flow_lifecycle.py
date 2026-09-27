"""Shared flow-lifetime lock paths for execution and artifact management."""
from pathlib import Path

from agent_bridge.storage import FileLock, identifier
from role_review import safe

FLOW_LOCK_SCHEMA = 1


def lock_path(project, config, flow):
    project = Path(project).resolve()
    identifier(flow)
    flows = safe(project, '.role-flows')
    state = safe(project, config['state'].relative_to(project).as_posix())
    if state.is_relative_to(flows) or flows.is_relative_to(state):
        raise ValueError('UNSUPPORTED_STATE_LAYOUT: parent state overlaps role flows')
    safe(project, '.role-flows/' + flow)
    return safe(project, (state / 'flow-locks' / (flow + '.lock')).relative_to(project).as_posix())


def lock(project, config, flow):
    return FileLock(lock_path(project, config, flow))
