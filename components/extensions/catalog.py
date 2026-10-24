# SPDX-License-Identifier: MIT
"""Register declarative optional components before invoking the existing manager."""
import json
from pathlib import Path


def register(manager):
    root = manager.ROOT
    for path in sorted((root / 'components').glob('*/extension.json')):
        item = json.loads(path.read_text(encoding='utf-8'))
        if set(item) != {'name', 'client_plugin', 'compose_files'}:
            raise manager.OperationError(f'Invalid extension descriptor: {path.name}')
        name, plugin, compose = item['name'], item['client_plugin'], item['compose_files']
        if not isinstance(name, str) or not name.replace('_', '').isalnum():
            raise manager.OperationError('Invalid extension name')
        if name in manager.EXTENSION_COMPOSE or not isinstance(compose, list):
            raise manager.OperationError(f'Duplicate/invalid extension: {name}')
        if not isinstance(plugin, str) or not plugin.startswith('plugins/'):
            raise manager.OperationError(f'Invalid UI plugin: {name}')
        for value in [f'cvat-ui/{plugin}', *compose]:
            if not isinstance(value, str) or Path(value).is_absolute() or '..' in Path(value).parts:
                raise manager.OperationError(f'Invalid extension path: {name}')
            if not (root / value).exists():
                raise manager.OperationError(f'Missing extension path: {value}')
        manager.EXTENSION_COMPOSE[name] = tuple(compose)
        manager.EXTENSION_PLUGINS[name] = plugin
        manager.COMPOSE_FILES = tuple(dict.fromkeys((*manager.COMPOSE_FILES, *compose)))
