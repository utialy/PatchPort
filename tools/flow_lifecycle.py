"""Shared flow-lifetime lock paths for execution and artifact management."""
from pathlib import Path
import contextlib

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


@contextlib.contextmanager
def lock(project, config, flow, *, executing=False):
    from agent_bridge.records_flow import require
    require(config,flow,write=True,allow_active=executing)
    with FileLock(lock_path(project, config, flow)):
        require(config,flow,write=True,allow_active=executing)
        yield
