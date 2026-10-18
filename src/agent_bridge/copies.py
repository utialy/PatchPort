"""Paginated, read-only inventory of ordinary and role-owned task copies."""
import sqlite3

from . import cleanup
from .storage import checked_local_path, identifier

SCHEMA = 1


def listing(config, kind='batch', days=30, cursor='', limit=25):
    if kind not in ('batch', 'role') or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid copy inventory selection')
    if type(days) is not int or not 1 <= days <= 3650 or not isinstance(cursor, str) or len(cursor) > 300:
        raise ValueError('Invalid copy inventory cursor or retention period')
    if kind == 'batch':
        return dict(schema=SCHEMA, **cleanup.observe_batch(config, days, after=cursor, limit=limit))
    from .p1 import helper, role_config
    role_config(config)
    root = checked_local_path(config['root'] / '.role-flows')
    if cursor:
        flow_id, role = cursor.rsplit(':', 1)
        identifier(flow_id)
        if role not in ('developer', 'reviewer'):
            raise ValueError('Invalid role cursor')
    # A directory entry is only a candidate name; resolve verifies its ownership.
    keys = []
    if root.exists():
        for folder in root.iterdir():
            try:
                identifier(folder.name)
            except ValueError:
                continue
            for role in ('developer', 'reviewer'):
                token = folder.name + ':' + role
                if token > cursor:
                    keys.append((token, folder.name, role))
    keys.sort()
    page, more = keys[:limit], len(keys) > limit
    items = []
    for token, flow, role in page:
        item = dict(kind='role', flow_id=flow, role=role, task_id=None, state='UNKNOWN',
                    artifact=dict(eligible=False, bytes=None, reason='ROLE_NOT_READY_OR_UNSAFE'), archive='UNKNOWN')
        try:
            _, _, _, saved = helper('role_review').flow_header(config['root'], flow)
            task = identifier(saved[role]['task'])
            report = helper('role_manage').preview_prune(config['root'], flow, role, task, days, freeze=False)
            item.update(task_id=task, state=report['artifact']['state'], artifact=report['artifact'],
                        archive=report['archive'], queue_mode=report['queue_mode'])
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            pass
        items.append(item)
    return dict(schema=SCHEMA, kind=kind, days=days, items=items, more=more,
                next_cursor=page[-1][0] if more else None, read_only=True,
                scope='role_task_copies_in_selected_project')
