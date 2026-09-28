"""Shared validation for explicitly read-only role reviewers."""

def readonly(endpoint):
    command = endpoint.get('command', [])
    if endpoint.get('adapter') == 'codex':
        return endpoint.get('sandbox') == 'read-only' and not any('dangerously' in arg or 'yolo' in arg for arg in command)
    if endpoint.get('adapter') != 'claude':
        return False
    return (command.count('--tools') == 1 and command.index('--tools') + 1 < len(command) and command[command.index('--tools') + 1] == 'Read'
            and not any('dangerously' in arg or 'bypassPermissions' in arg or arg.startswith('--tools=') for arg in command))
