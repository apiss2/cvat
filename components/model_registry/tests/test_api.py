import json
import time
import threading
from registry.schema import function_id
from conftest import ROOT,TOKENS,headers,upload,model_files

def test_user_and_service_auth_are_separate(ctx):
    c=ctx['client']
    assert c.get('/api/models').status_code==401
    assert c.get('/api/models',headers=headers('service')).status_code==401
    assert c.get('/internal/functions',headers=headers()).status_code==401
    assert c.get('/api/me',headers=headers()).json()=={'id':1,'name':'alice','admin':False,'auth_mode':'cvat'}
    assert c.get('/internal/functions',headers=headers('service')).json()=={}

def test_auth_before_large_upload(ctx):
    r=ctx['client'].post('/api/upload',headers={'Content-Length':str(3*1024**3)},content=b'x')
    assert r.status_code==401
    r=ctx['client'].post('/api/upload',headers={**headers(),'Content-Length':str(3*1024**3)},content=b'x')
    assert r.status_code==413

def test_publish_infer_and_logs(ctx,published,sample):
    c=ctx['client'];op=published;mid=op['model_id'];fid=function_id(mid,op['revision'])
    assert fid in c.get('/internal/functions',headers=headers('service')).json()
    r=c.post('/internal/functions/'+fid+'/invoke',headers=headers('service'),json={'image':sample})
    assert r.status_code==200 and r.json()[0]['type']=='polygon'
    assert len(r.json()[0]['points'])>=6 and len(r.json()[0]['points'])%2==0
    assert len(r.headers['x-request-id'])==32
    logs=c.get(f'/api/models/{mid}/logs',headers=headers()).json()
    assert any(e['stage']=='publish' for e in logs) and any(e['stage']=='predict' for e in logs)
    assert all(sample not in e['message'] for e in logs)
    assert 'attachment' in c.get(f'/api/models/{mid}/logs.txt',headers=headers()).headers['content-disposition']

def test_ownership_enforcement(ctx,published,sample):
    c=ctx['client'];mid=published['model_id']
    for path in [f'/api/models/{mid}/logs',f'/api/models/{mid}/logs.txt',f'/api/operations/{published["id"]}']:
        assert c.get(path,headers=headers('bob')).status_code==403
        assert c.get(path,headers=headers('admin')).status_code==200
    assert c.post(f'/api/models/{mid}/test',headers=headers('bob'),json={'image':sample}).status_code==403
    assert c.request('DELETE',f'/api/models/{mid}',headers=headers('bob'),json={'expected_revision':published['revision']}).status_code==403
    public=c.get(f'/api/models/{mid}',headers=headers('bob'))
    assert public.status_code==200
    public=public.json()
    assert public['owner']=='alice' and public['can_manage'] is False
    assert public['manifest'] and 'operations' not in public and 'revisions' not in public
    assert c.get('/api/models',headers=headers('bob')).json()==[public]
    assert c.get(f'/api/models/{mid}',headers=headers()).json()['can_manage'] is True
    assert c.post(f'/api/models/{mid}/rollback',headers=headers('bob'),json={'expected_revision':published['revision'],'revision':published['revision']}).status_code==403
    update=c.post('/api/upload',headers=headers('bob'),data={'model_id':mid,'expected_revision':published['revision'],'manifest':(ROOT/'examples/segmentation/manifest.json').read_text(encoding='utf-8')},files=model_files())
    assert update.status_code==403

def test_update_failed_keeps_active(ctx,published):
    c=ctx['client'];mid=published['model_id'];old=published['revision']
    ctx['runtime'].fail_next=True
    result=upload(ctx,model_id=mid,expected=old)
    assert result['status']=='failed'
    assert c.get(f'/api/models/{mid}',headers=headers()).json()['active_revision']==old
    assert len(ctx['service'].store.revisions(mid))==1
    assert 'TEST load/predict error' in c.get(f'/api/models/{mid}/logs.txt',headers=headers()).text

def test_successful_update_pins_old_revision_and_rollback(ctx,published,sample):
    c=ctx['client'];mid=published['model_id'];old=published['revision']
    new=upload(ctx,model_id=mid,expected=old);assert new['status']=='succeeded'
    catalog=c.get('/internal/functions',headers=headers('service')).json()
    assert function_id(mid,new['revision']) in catalog and function_id(mid,old) not in catalog
    assert c.get('/internal/functions/'+function_id(mid,old),headers=headers('service')).status_code==200
    r=c.post('/internal/functions/'+function_id(mid,old)+'/invoke',headers=headers('service'),json={'image':sample})
    assert r.status_code==200 and ctx['runtime'].calls[-1][1]==old
    r=c.post(f'/api/models/{mid}/rollback',headers=headers(),json={'expected_revision':new['revision'],'revision':old})
    assert r.status_code==200 and r.json()['active_revision']==old

def test_delete_tombstone_and_stale_guard(ctx,published,sample):
    c=ctx['client'];mid=published['model_id'];rev=published['revision'];fid=function_id(mid,rev)
    assert c.request('DELETE',f'/api/models/{mid}',headers=headers(),json={'expected_revision':'wrong'}).status_code==409
    assert c.request('DELETE',f'/api/models/{mid}',headers=headers(),json={'expected_revision':rev}).status_code==200
    assert mid in ctx['runtime'].revoked
    assert fid not in c.get('/internal/functions',headers=headers('service')).json()
    assert c.get('/internal/functions/'+fid,headers=headers('service')).status_code==410
    assert c.post('/internal/functions/'+fid+'/invoke',headers=headers('service'),json={'image':sample}).status_code==410
    assert (ctx['settings'].data_dir/'packages'/mid/rev/'model.onnx').exists()

def test_individual_file_upload(ctx):
    d=ROOT/'examples/detection';files=[('code',('model.py',(d/'model.py').read_bytes(),'text/plain')),('weights',('model.onnx',(d/'model.onnx').read_bytes(),'application/octet-stream')),('sample',('sample.png',(d/'sample.png').read_bytes(),'image/png'))]
    r=ctx['client'].post('/api/upload',headers=headers(),data={'manifest':(d/'manifest.json').read_text()},files=files)
    assert r.status_code==202,r.text
    opid=r.json()['id']
    for _ in range(500):
        op=ctx['client'].get('/api/operations/'+opid,headers=headers()).json()
        if op['status'] in ('failed','succeeded'):break
        time.sleep(.01)
    assert op['status']=='succeeded',op


def test_external_zip_registration_and_full_update_are_removed(ctx,published):
    c=ctx['client']
    before=ctx['service'].store.models()
    for data in ({}, {'model_id':published['model_id'],'expected_revision':published['revision']}):
        response=c.post('/api/models',headers=headers(),data=data,files={'package':('model.zip',b'invalid')})
        assert response.status_code==405
    response=c.post('/api/upload',headers=headers(),files={'package':('model.zip',b'invalid')})
    assert response.status_code==422
    assert ctx['service'].store.models()==before
    assert not list((ctx['settings'].data_dir/'uploads').iterdir())

def test_disable_user_applies_without_restart(ctx):
    ctx['cvat_users']['alice']['is_active'] = False
    assert ctx['client'].get('/api/me',headers=headers()).status_code==403

def test_validation_errors_do_not_echo_image(ctx,published,sample):
    r=ctx['client'].post(f'/api/models/{published["model_id"]}/test',headers=headers(),json={'image':sample,'threshold':2})
    assert r.status_code==422 and sample not in r.text

def test_static_ui_and_csp(ctx):
    c=ctx['client'];r=c.get('/');assert r.status_code==200 and "frame-ancestors 'none'" in r.headers['content-security-policy']
    assert c.get('/static/app.js').status_code==200
    assert c.get('/openapi.json').status_code==404

def test_concurrent_updates_only_one_published(ctx,published):
    c=ctx['client'];mid=published['model_id'];old=published['revision']
    block=threading.Event();ctx['runtime'].block=block
    operations=[]
    for _ in range(2):
        r=c.post('/api/upload',headers=headers(),data={'model_id':mid,'expected_revision':old,'manifest':(ROOT/'examples/segmentation/manifest.json').read_text(encoding='utf-8')},files=model_files())
        assert r.status_code==202;operations.append(r.json()['id'])
    block.set()
    for _ in range(500):
        states=[c.get('/api/operations/'+o,headers=headers()).json()['status'] for o in operations]
        if all(s in ('failed','succeeded') for s in states):break
        time.sleep(.01)
    assert sorted(states)==['failed','succeeded']
    assert len(ctx['service'].store.revisions(mid))==2


def test_public_catalog_never_exposes_failed_update_or_private_paths(ctx,published):
    c=ctx['client'];mid=published['model_id'];old=published['revision']
    ctx['runtime'].fail_next=True
    failed=upload(ctx,model_id=mid,expected=old)
    assert failed['status']=='failed'
    private=c.get(f'/api/models/{mid}',headers=headers()).json()
    assert 'TEST load/predict error' in json.dumps(private)
    public=c.get(f'/api/models/{mid}',headers=headers('bob')).json()
    assert public['active_revision']==old
    assert set(public)=={'id','owner','active_revision','deleted','created_at','manifest','can_manage'}
    assert all(value not in json.dumps(public) for value in ('Traceback','TEST load/predict error','packages/'))
    assert c.get('/api/models',headers=headers('bob')).json()==[public]


def test_other_users_do_not_see_drafts_or_deleted_models(ctx,published):
    c=ctx['client'];mid=published['model_id']
    ctx['runtime'].fail_next=True
    failed=upload(ctx)
    assert failed['status']=='failed'
    assert c.get('/api/models/'+failed['model_id'],headers=headers('bob')).status_code==404
    assert len(c.get('/api/models',headers=headers('bob')).json())==1
    assert c.request('DELETE',f'/api/models/{mid}',headers=headers(),json={'expected_revision':published['revision']}).status_code==200
    assert c.get(f'/api/models/{mid}',headers=headers('bob')).status_code==404
    assert c.get('/api/models',headers=headers('bob')).json()==[]
    assert len(c.get('/api/models',headers=headers()).json())==2
    assert len(c.get('/api/models',headers=headers('admin')).json())==2
