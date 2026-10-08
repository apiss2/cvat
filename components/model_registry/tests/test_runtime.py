import json
import threading
from dataclasses import replace
from pathlib import Path
import httpx
import pytest
from registry.config import Settings
from registry.runtime import DockerRuntime,Entry,Busy,RuntimeFailure,limited_json,DockerEngine

class Engine:
    def __init__(self):self.calls=[]
    def request(self,*args,**kwargs):self.calls.append((args,kwargs));return []
    def logs(self,*args):return 'stdout test'
    def close(self):pass

@pytest.fixture
def runtime(tmp_path,monkeypatch):
    engine=Engine();s=Settings(auth_mode='cvat',public_url='http://testserver/model-registry/',data_dir=tmp_path,host_data_dir='/srv/models',max_loaded=2)
    r=DockerRuntime(s,engine)
    def load(entry):entry.container=entry.revision
    monkeypatch.setattr(r,'_load',load)
    monkeypatch.setattr('registry.runtime.limited_json',lambda *a,**k:[])
    yield r
    r.close()

def test_worker_spec_isolated(tmp_path):
    settings=Settings(auth_mode='cvat',public_url='http://testserver/model-registry/',data_dir=tmp_path,host_data_dir='/srv/models')
    runtime=DockerRuntime(settings,Engine());entry=Entry('a'*20,'b'*16,tmp_path/'packages/m/r',tmp_path/'run/m-r')
    spec=runtime.spec(entry);host=spec['HostConfig']
    assert spec['User']=='65532:65532' and host['NetworkMode']=='none'
    assert host['ReadonlyRootfs'] and host['CapDrop']==['ALL'] and host['PidsLimit']==128
    assert host['Memory']==4*1024**3 and host['MemorySwap']==host['Memory']
    assert host['Mounts'][0]['ReadOnly'] and host['Mounts'][0]['Source']=='/srv/models/packages/m/r'
    assert len(host['Mounts'])==2 and 'DeviceRequests' not in host
    assert all('token' not in x.lower() and 'docker.sock' not in x.lower() for x in spec['Env'])
    assert '/var/run/docker.sock' not in json.dumps(host['Mounts'])

def test_gpu_is_one_explicit_device(tmp_path):
    s=Settings(auth_mode='cvat',public_url='http://testserver/model-registry/',data_dir=tmp_path,host_data_dir='/srv/models',gpu_device='1');s.validate()
    r=DockerRuntime(s,Engine());entry=Entry('m','r',tmp_path/'p',tmp_path/'run')
    req=r.spec(entry)['HostConfig']['DeviceRequests'];assert req[0]['DeviceIDs']==['1']
    with pytest.raises(ValueError):replace(s,gpu_device='all').validate()

def test_lru_evicts_only_idle(runtime):
    for i in ['a','b','a','c']:runtime.call('m',i,runtime.settings.data_dir/i,{},'rid')
    assert set(runtime.entries)=={'m-a','m-c'}
    assert any(a[0]=='DELETE' and a[1]=='/containers/b' for a,k in runtime.engine.calls)

def test_busy_revision_and_busy_capacity(runtime):
    for i in ['a','b']:
        runtime.entries['m-'+i]=Entry('m',i,runtime.settings.data_dir/i,runtime.settings.data_dir/'run'/i,busy=True)
    with pytest.raises(Busy):runtime.call('m','a',runtime.settings.data_dir/'a',{},'rid')
    with pytest.raises(Busy):runtime.call('m','c',runtime.settings.data_dir/'c',{},'rid')

def test_failure_stops_worker_and_captures_logs(runtime,monkeypatch):
    def fail(*a,**k):raise RuntimeFailure('bad output')
    monkeypatch.setattr('registry.runtime.limited_json',fail)
    with pytest.raises(RuntimeFailure,match='stdout test'):runtime.call('m','a',runtime.settings.data_dir/'a',{},'rid')
    assert not runtime.entries

def test_revoke_does_not_kill_accepted_frame(runtime,monkeypatch):
    start=threading.Event();finish=threading.Event();result=[]
    def wait(*a,**k):start.set();assert finish.wait(3);return []
    monkeypatch.setattr('registry.runtime.limited_json',wait)
    t=threading.Thread(target=lambda:result.append(runtime.call('m','a',runtime.settings.data_dir/'a',{},'rid')))
    t.start();assert start.wait(3);runtime.revoke('m');assert runtime.entries['m-a'].busy
    finish.set();t.join(3);assert result==[[]] and not runtime.entries
    with pytest.raises(RuntimeFailure,match='deleted'):runtime.call('m','b',runtime.settings.data_dir/'b',{},'rid')

def test_successful_worker_logs_reach_sink(runtime):
    out=[];runtime.log_sink=lambda *args:out.append(args)
    runtime.call('m','a',runtime.settings.data_dir/'a',{},'rid')
    assert out==[('m','a','rid','stdout test')]

def test_response_size_and_json_limits():
    with httpx.Client(base_url='http://x',transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*20))) as c:
        with pytest.raises(RuntimeFailure,match='byte limit'):limited_json(c,'GET','/',limit=10)
        with pytest.raises(RuntimeFailure,match='JSON'):limited_json(c,'GET','/')

def test_reconcile_filters_only_own_instance(runtime):
    runtime.reconcile();args,kw=runtime.engine.calls[-1]
    assert args==('GET','/containers/json')
    assert json.loads(kw['params']['filters'])=={'label':['org.cvat-model-registry.instance=team-models']}

def test_docker_multiplexed_logs_are_decoded():
    e=DockerEngine('/nonexistent');e.client.close();e.prefix='/v1.47'
    chunk=b'hello\n';body=b'\x01\0\0\0'+len(chunk).to_bytes(4,'big')+chunk
    e.client=httpx.Client(base_url='http://docker',transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body)))
    assert e.logs('id')=='hello\n';e.close()
