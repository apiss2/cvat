import json
import sys
import types
from pathlib import Path
import numpy as np
import pytest
from registry.sdk import ModelContext,ModelBase
from registry.schema import Manifest
from conftest import ROOT

def test_sdk_rejects_undeclared_weight():
    m=Manifest.model_validate_json((ROOT/'examples/segmentation/manifest.json').read_text())
    ctx=ModelContext(Path('/model'),m,('CPUExecutionProvider',))
    with pytest.raises(ValueError):ctx.create_session('../other.onnx')

def test_sdk_rejects_missing_provider(monkeypatch):
    fake=types.SimpleNamespace(get_available_providers=lambda:['CPUExecutionProvider'])
    monkeypatch.setitem(sys.modules,'onnxruntime',fake)
    m=Manifest.model_validate_json((ROOT/'examples/segmentation/manifest.json').read_text())
    with pytest.raises(RuntimeError,match='providers unavailable'):ModelContext(Path('/model'),m,('CUDAExecutionProvider',)).create_session('model.onnx')

def test_sdk_rejects_silent_provider_fallback(monkeypatch):
    fake=types.SimpleNamespace(get_available_providers=lambda:['CPUExecutionProvider','CUDAExecutionProvider'],SessionOptions=lambda:types.SimpleNamespace(),InferenceSession=lambda *a,**k:types.SimpleNamespace(get_providers=lambda:['CPUExecutionProvider']))
    monkeypatch.setitem(sys.modules,'onnxruntime',fake)
    m=Manifest.model_validate_json((ROOT/'examples/segmentation/manifest.json').read_text())
    with pytest.raises(RuntimeError,match='fallback'):ModelContext(Path('/model'),m,('CUDAExecutionProvider','CPUExecutionProvider')).create_session('model.onnx')

@pytest.mark.parametrize('kind',['segmentation','detection'])
def test_real_onnx_demo(kind,sample):
    # This is a real ONNX checker and CPU inference test, not a fake inference test.
    # It is explicitly skipped when the optional runtime packages are absent.
    pytest.importorskip('onnx',reason='ONNX package is not installed in this environment')
    pytest.importorskip('onnxruntime',reason='ONNX Runtime is not installed in this environment')
    from fastapi.testclient import TestClient
    from registry.worker import create_worker
    with TestClient(create_worker(ROOT/'examples'/kind)) as c:
        assert c.get('/health').json()['ready']
        r=c.post('/predict',json={'image':sample})
        assert r.status_code==200,r.text
        assert len(r.json())==1
        if kind=='detection':assert r.json()[0]['points']==[12.,8.,44.,32.]
        else:
            result=r.json()[0]
            assert result['type']=='polygon' and 'mask' not in result
            points=np.asarray(result['points']).reshape(-1,2)
            np.testing.assert_allclose(points.min(axis=0),[12.,8.])
            np.testing.assert_allclose(points.max(axis=0),[43.,31.])
            assert np.min(np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1))>=2.-1e-9
        # CVAT may send this compatibility field; neither SDK nor codec uses it.
        assert c.post('/predict',json={'image':sample,'threshold':0.}).json()==r.json()
        assert c.post('/predict',json={'image':sample,'threshold':1.}).json()==r.json()
