# SPDX-License-Identifier: MIT
"""Small, testable contracts for native CVAT labels and frame metadata."""
from . import DicomImportError

LABEL_NAME = 'DicomTag'


def missing_attributes(existing, names):
    if existing is not None and existing.get('type') not in ('tag', 'any'):
        raise DicomImportError('Existing DicomTag label is not a tag label')
    current = {attribute['name']: attribute for attribute in (existing or {}).get('attributes', [])}
    for name in set(names) & current.keys():
        if current[name]['input_type'] != 'text':
            raise DicomImportError(f'Existing DicomTag attribute {name} is not text')
    return [{'name': name, 'input_type': 'text', 'mutable': False, 'default_value': '', 'values': []}
            for name in sorted(set(names) - current.keys())]


def image_frames(tags, images):
    seen = set()
    for path, frame in images:
        # initialize_task may prepend a source directory. Each task has unique basenames.
        name = path.replace('\\', '/').rsplit('/', 1)[-1]
        if name not in tags or name in seen:
            raise DicomImportError('CVAT image order does not match the converted files')
        seen.add(name)
        yield int(frame), tags[name]
    if seen != set(tags):
        raise DicomImportError('CVAT did not register all converted images')


def tag_batches(frames, label_id, attribute_ids, limit=100):
    tags = []
    count = 0
    for frame, values in frames:
        tag = {'frame': frame, 'label_id': label_id, 'group': 0, 'source': 'auto',
               'attributes': [{'spec_id': attribute_ids[key], 'value': value} for key, value in values.items()]}
        if tags and (len(tags) >= limit or count + len(values) > 10000):
            yield {'tags': tags, 'shapes': [], 'tracks': []}
            tags, count = [], 0
        tags.append(tag)
        count += len(values)
    if tags:
        yield {'tags': tags, 'shapes': [], 'tracks': []}
