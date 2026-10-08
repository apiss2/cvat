import json
import httpx
import pytest
from fastapi.testclient import TestClient
from registry.gateway import create_gateway,safe_origin
from registry.schema import function_metadata,function_id
from conftest import ROOT,TOKENS

MID='1'*20;REV='2'*16;FID=function_id(MID,REV)
META=function_metadata(MID,REV,json.loads((ROOT/'examples/segmentation/manifest.json').read_text()),'nuclio')
SAM={'metadata':{'name':'sam2-video','annotations':{'type':'tracker'}},'spec':{'description':'SAM2'},'status':{'httpPort':3456}}

def make(tmp_path, registry_handler=None, legacy_handler=None, token=True):
    calls=[]
    def default_registry(req):
        calls.append(('registry',req))
        assert req.headers['authorization']=='Bearer '+TOKENS['service']
        if req.url.path=='/internal/functions':return httpx.Response(200,json={FID:META})
        if req.url.path.endswith('/invoke'):return httpx.Response(200,json=[],headers={'x-request-id':'a'*32})
        return httpx.Response(200,json=META)
    def default_legacy(req):
        calls.append(('nuclio',req))
        if req.url.path=='/api/functions':return httpx.Response(200,json={'actual-key-can-differ':SAM})
        if req.method=='POST':return httpx.Response(200,json={'states':['untouched']})
        return httpx.Response(200,json=SAM)
    secret=tmp_path/'token'
    if token:secret.write_text(TOKENS['service'])
    app=create_gateway(token_file=secret,registry_client=httpx.Client(base_url='http://registry',transport=httpx.MockTransport(registry_handler or default_registry)),nuclio_client=httpx.Client(base_url='http://nuclio',transport=httpx.MockTransport(legacy_handler or default_legacy)))
    return TestClient(app),calls

def test_merge_catalog_and_metadata(tmp_path):
    c,calls=make(tmp_path)
    with c:
        assert set(c.get('/api/functions').json())=={FID,'sam2-video'}
        assert c.get('/api/functions/'+FID).json()==META
        assert c.get('/api/functions/sam2-video').json()==SAM
        assert c.get('/api/functions',headers={'x-nuclio-function-namespace':'wrong'}).status_code==400

def test_legacy_body_and_headers_preserved(tmp_path):
    c,calls=make(tmp_path)
    raw=b'{"image":"img", "pos_points":[[1,2]], "states":[{"token":"xyz"}],"custom":[1,2]}'
    with c:
        r=c.post('/api/function_invocations',content=raw,headers={'x-nuclio-function-name':'sam2-video','x-nuclio-invoke-timeout':'300s'})
        assert r.status_code==200 and r.json()=={'states':['untouched']}
        target,req=calls[-1];assert target=='nuclio' and req.content==raw
        assert req.headers['x-nuclio-function-name']=='sam2-video' and req.headers['x-nuclio-path']=='/'
        assert 'authorization' not in req.headers

def test_registry_invocation_auth_and_strict_input(tmp_path,sample):
    c,calls=make(tmp_path)
    with c:
        r=c.post('/api/function_invocations',json={'image':sample,'threshold':0},headers={'x-nuclio-function-name':FID})
        assert r.status_code==200 and r.headers['x-request-id']=='a'*32
        assert calls[-1][0]=='registry' and json.loads(calls[-1][1].content)['threshold']==0
        r=c.post('/api/function_invocations',json={'image':sample,'unexpected':True},headers={'x-nuclio-function-name':FID})
        assert r.status_code==422

def test_registry_outage_does_not_hide_sam2(tmp_path):
    c,_=make(tmp_path,registry_handler=lambda r:httpx.Response(503))
    with c:
        r=c.get('/api/functions');assert set(r.json())=={'sam2-video'}
        assert r.headers['x-model-catalog-unavailable']=='registry'

def test_legacy_outage_does_not_hide_registry(tmp_path):
    c,_=make(tmp_path,legacy_handler=lambda r:httpx.Response(503))
    with c:
        r=c.get('/api/functions');assert set(r.json())=={FID}
        assert r.headers['x-model-catalog-unavailable']=='nuclio'

def test_missing_secret_still_serves_sam2(tmp_path):
    c,_=make(tmp_path,token=False)
    with c:
        assert set(c.get('/api/functions').json())=={'sam2-video'}
        assert c.get('/api/functions/'+FID).status_code==503

def test_both_outages_return_error(tmp_path):
    c,_=make(tmp_path,registry_handler=lambda r:httpx.Response(503),legacy_handler=lambda r:httpx.Response(503))
    with c:assert c.get('/api/functions').status_code==502

def test_malformed_source_does_not_leak_partial_catalog(tmp_path):
    c,_=make(tmp_path,legacy_handler=lambda r:httpx.Response(200,json={'a':SAM,'bad':{}}))
    with c:
        r=c.get('/api/functions');assert set(r.json())=={FID}

def test_reserved_prefix_collision_is_loud(tmp_path):
    c,_=make(tmp_path,legacy_handler=lambda r:httpx.Response(200,json={FID:META}))
    with c:assert c.get('/api/functions').status_code==409

def test_timeout_maps_to_504(tmp_path):
    def timeout(req):raise httpx.ReadTimeout('timeout',request=req)
    c,_=make(tmp_path,registry_handler=timeout)
    with c:assert c.get('/api/functions/'+FID).status_code==504

@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:pass@host','http://x/path','http://x?token=x','http://x#fragment'])
def test_upstream_origin_validation(url):
    with pytest.raises(ValueError):safe_origin(url)
