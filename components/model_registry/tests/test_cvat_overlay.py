"""Configuration-contract tests for the only two pre-existing custom files changed."""
import copy
import importlib.util
from pathlib import Path
import pytest
from conftest import ROOT

repo=ROOT.parents[1]
path=repo/'components/extensions/manage.py'
if not path.exists():path=repo.parent/'patches/replacements/components/extensions/manage.py'
if not path.exists():pytest.skip("CVAT custom-component source is not present in this standalone copy",allow_module_level=True)
spec=importlib.util.spec_from_file_location('registry_extension_contract',path);ops=importlib.util.module_from_spec(spec);spec.loader.exec_module(ops)

def config(enabled=True):
    env={'CVAT_EXT_SERVER_IMAGE':'cvat-local/server:test','CVAT_EXT_UI_IMAGE':'cvat-local/ui:test','CVAT_NETWORK_NAME':'cvat_cvat','SAM2_REDIS_VOLUME':'cvat_sam2_redis_data','NUCLIO_NAMESPACE':'nuclio','CVAT_MODEL_REGISTRY_ENABLED':'1' if enabled else '0'}
    route={'CVAT_NUCLIO_INVOKE_METHOD':'dashboard','CVAT_NUCLIO_FUNCTION_NAMESPACE':'nuclio','CVAT_NUCLIO_HOST':'model-gateway','CVAT_NUCLIO_PORT':'8070','CVAT_NUCLIO_DEFAULT_TIMEOUT':'300'}
    services={name:{'image':env['CVAT_EXT_SERVER_IMAGE'],'pull_policy':'never','environment':route.copy()} for name in ('cvat_server','cvat_worker_annotation','cvat_worker_import','cvat_worker_export')}
    services['cvat_ui']={'image':env['CVAT_EXT_UI_IMAGE'],'pull_policy':'never','build':{'dockerfile':'Dockerfile.ui','args':{'CLIENT_PLUGINS':'plugins/sam2:plugins/model-registry'}}}
    services['sam2_redis']={'image':'redis:test'};services['nuclio']={'image':'nuclio:test','ports':[{'host_ip':'127.0.0.1'}]}
    services['model-gateway']={'image':'cvat-local/model-gateway:1.0.0','pull_policy':'never'}
    env.update(MR_MANAGER_IMAGE='cvat-local/model-registry:test',MR_WORKER_IMAGE='cvat-local/onnx-worker:test')
    services['model-registry']={'image':env['MR_MANAGER_IMAGE'],'pull_policy':'never','networks':{'cvat':{}},'environment':{'MR_WORKER_IMAGE':env['MR_WORKER_IMAGE']}}
    return {'services':services,'networks':{'cvat':{'external':True,'name':'cvat_cvat'}},'volumes':{'sam2_redis_data':{'external':True,'name':'cvat_sam2_redis_data'}}},env

def test_complete_registry_configuration_passes():
    model,env=config();ops.validate_model(model,env)

@pytest.mark.parametrize('which',['cvat_server','cvat_worker_annotation'])
def test_both_cvat_routes_are_required(which):
    model,env=config();model['services'][which]['environment']['CVAT_NUCLIO_HOST']='nuclio'
    with pytest.raises(ops.OperationError,match='route'):ops.validate_model(model,env)

@pytest.mark.parametrize('plugins',['plugins/sam2','plugins/model-registry'])
def test_both_plugins_are_required(plugins):
    model,env=config();model['services']['cvat_ui']['build']['args']['CLIENT_PLUGINS']=plugins
    with pytest.raises(ops.OperationError):ops.validate_model(model,env)

def test_exposing_gateway_is_rejected():
    model,env=config();model['services']['model-gateway']['ports']=['8071:8070']
    with pytest.raises(ops.OperationError):ops.validate_model(model,env)

def test_exposing_manager_is_rejected():
    model,env=config();model['services']['model-registry']['ports']=['8091:8091']
    with pytest.raises(ops.OperationError,match='host ports'):ops.validate_model(model,env)

def test_changing_worker_image_is_rejected():
    model,env=config();model['services']['model-registry']['environment']['MR_WORKER_IMAGE']='unselected:latest'
    with pytest.raises(ops.OperationError,match='worker image'):ops.validate_model(model,env)

def test_old_configuration_remains_allowed_when_disabled():
    model,env=config(False);del model['services']['model-gateway'];model['services']['cvat_ui']['build']['args']['CLIENT_PLUGINS']='plugins/sam2'
    for name in ('cvat_server','cvat_worker_annotation'):model['services'][name]['environment']['CVAT_NUCLIO_HOST']='nuclio'
    ops.validate_model(model,env)
