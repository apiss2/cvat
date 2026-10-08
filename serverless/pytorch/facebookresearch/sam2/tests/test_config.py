# SPDX-License-Identifier: MIT
from pathlib import Path
import yaml
import pytest
ROOT=Path(__file__).resolve().parents[1]/'nuclio'

@pytest.mark.parametrize('name',['function-gpu.yaml','tracker-gpu.yaml'])
def test_gpu_function_configuration(name):
    config=yaml.safe_load((ROOT/name).read_text())
    assert config['metadata']['annotations']['spec']=='[]'
    spec=config['spec']
    assert spec['resources']['limits']['nvidia.com/gpu']==1
    assert spec['minReplicas']==spec['maxReplicas']==1
    assert spec['runtime']=='python:3.11'
    assert spec['triggers']['http']['maxWorkers']==1
    assert spec['handler'] in ('main:handler','tracker_main:handler')
    assert '2b90b9f5ceec907a1c18123530e92e794ad901a4' in str(spec)
    assert 'torch==2.6.0' in str(spec)
    assert 'SAM2_BUILD_CUDA=0' in str(spec)

def test_distinct_ids_and_typed_polygon_contract():
    image=yaml.safe_load((ROOT/'function-gpu.yaml').read_text())
    video=yaml.safe_load((ROOT/'tracker-gpu.yaml').read_text())
    assert image['metadata']['name']!=video['metadata']['name']
    assert image['metadata']['name']!='pth-facebookresearch-sam-vit-h'
    assert video['metadata']['annotations']['supported_shape_types']=='polygon'
    assert image['metadata']['annotations']['startswith_box_optional']=='true'


def test_redis_configuration_does_not_force_process_local_sessions():
    spec=yaml.safe_load((ROOT/'tracker-gpu.yaml').read_text())['spec']
    env={item['name']:item['value'] for item in spec['env']}
    assert env['SAM2_SESSION_TTL_SECONDS']=='28800'
    assert 'SAM2_MAX_FRAMES' not in env and 'SAM2_MAX_SESSIONS' not in env
    assert 'redis==5.2.1' in str(spec) and 'safetensors==0.7.0' in str(spec)
    main=(ROOT/'tracker_main.py').read_text()
    assert 'RedisTracker' in main and 'StreamingVideo' not in main
    assert not (ROOT/'sessions.py').exists()


def test_redis_is_dedicated_persistent_and_not_published():
    config=yaml.safe_load((ROOT.parents[4]/'components/sam2/docker-compose.redis.yml').read_text())
    service=config['services']['sam2_redis']
    assert 'ports' not in service
    assert 'sam2_redis_data:/data' in service['volumes']
    assert '--appendonly yes' in str(service['command'])
    assert '--appendfsync everysec' in str(service['command'])
    assert '--maxmemory-policy noeviction' in str(service['command'])
    assert '--requirepass' in str(service['command'])
