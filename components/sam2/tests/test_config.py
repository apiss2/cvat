# SPDX-License-Identifier: MIT
from pathlib import Path
import yaml
import pytest
from components.extensions.functions import render_function

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / 'serverless/pytorch/facebookresearch/sam2/nuclio'


def definition(name):
    return render_function(
        name, {'NUCLIO_NAMESPACE': 'test', 'SAM2_REDIS_PASSWORD': 'a' * 48},
        'sam2:test', ROOT,
    )


@pytest.mark.parametrize('name,handler', [
    ('pth-sam2-interactor', 'main:handler'),
    ('pth-sam2-tracker', 'tracker_main:handler'),
])
def test_gpu_function_configuration(name, handler):
    config=definition(name)
    assert config['metadata']['annotations']['spec']=='[]'
    spec=config['spec']
    assert spec['resources']['limits']['nvidia.com/gpu']==1
    assert spec['minReplicas']==spec['maxReplicas']==1
    assert spec['runtime']=='python:3.11'
    assert spec['triggers']['http']['maxWorkers']==1
    assert spec['handler'] == handler
    assert '2b90b9f5ceec907a1c18123530e92e794ad901a4' in str(spec)
    assert 'torch==2.6.0' in str(spec)
    assert 'SAM2_BUILD_CUDA=0' in str(spec)

def test_distinct_ids_and_typed_polygon_and_mask_contract():
    image=definition('pth-sam2-interactor')
    video=definition('pth-sam2-tracker')
    assert image['metadata']['name']!=video['metadata']['name']
    assert image['metadata']['name']!='pth-facebookresearch-sam-vit-h'
    assert set(video['metadata']['annotations']['supported_shape_types'].split(',')) == {'polygon', 'mask'}
    assert image['metadata']['annotations']['startswith_box_optional']=='true'


def test_redis_configuration_does_not_force_process_local_sessions():
    spec=definition('pth-sam2-tracker')['spec']
    env={item['name']:item['value'] for item in spec['env']}
    assert env['SAM2_SESSION_TTL_SECONDS']=='28800'
    assert 'SAM2_MAX_FRAMES' not in env and 'SAM2_MAX_SESSIONS' not in env
    assert 'redis==5.2.1' in str(spec) and 'safetensors==0.7.0' in str(spec)
    main=(RUNTIME/'tracker_main.py').read_text()
    assert 'RedisTracker' in main and 'StreamingVideo' not in main
    assert not (RUNTIME/'sessions.py').exists()


def test_redis_is_dedicated_persistent_and_not_published():
    config=yaml.safe_load((ROOT/'components/sam2/docker-compose.redis.yml').read_text())
    service=config['services']['sam2_redis']
    assert 'ports' not in service
    assert 'sam2_redis_data:/data' in service['volumes']
    assert '--appendonly yes' in str(service['command'])
    assert '--appendfsync everysec' in str(service['command'])
    assert '--maxmemory-policy noeviction' in str(service['command'])
    assert '--requirepass' in str(service['command'])
