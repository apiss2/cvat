import base64
import io
import json

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from registry.codec import decode_image, encode_results
from registry.schema import Manifest, InvokeRequest, function_metadata, function_id, parse_function_id
from registry.sdk import Box, Mask
from registry.wire import validate_wire
from conftest import ROOT

@pytest.fixture
def manifest():
    return Manifest(name='test', weights=['model.onnx'], labels=[{'id':8,'name':'object','type':'rectangle'}])

def test_boxes_right_bottom_exclusive(manifest):
    out=encode_results([Box(8,.8,(0,0,9,5))],manifest,(5,9,3))
    assert out[0]['points']==[0.,0.,9.,5.]
    validate_wire(out,manifest,9,5)

def test_detection_is_not_threshold_filtered(manifest):
    assert len(encode_results([Box(8,0,(0,0,1,1))],manifest,(2,2,3)))==1
    assert not encode_results([],manifest,(2,2,3))

@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,1.1,True,'0.9'])
def test_reject_bad_scores(manifest,bad):
    with pytest.raises((ValueError,TypeError)): encode_results([Box(8,bad,(0,0,1,1))],manifest,(2,2,3))

@pytest.mark.parametrize('box',[(-1,0,1,1),(0,0,10,1),(1,0,1,1),(0,0,1,6),(0,0,float('nan'),1),(False,0,1,1)])
def test_reject_bad_boxes(manifest,box):
    with pytest.raises(ValueError): encode_results([Box(8,.9,box)],manifest,(5,9,3))

def test_wrong_class_or_shape(manifest):
    for item in [Box(2,.9,(0,0,1,1)),Mask(8,.9,np.ones((2,2),bool)),Box(True,.9,(0,0,1,1))]:
        with pytest.raises(ValueError): encode_results([item],manifest,(2,2,3))
    with pytest.raises(ValueError): encode_results(np.ones((1,2,2),np.uint8),manifest,(2,2,3))

def test_wire_rejects_unknown_fields(manifest):
    with pytest.raises(ValueError): validate_wire([{'type':'rectangle','label':'object','confidence':1,'points':[0,0,1,1],'__extra':'x'}],manifest,9,5)

@pytest.mark.parametrize('field,value',[
    ('weights',['../x.onnx']),('labels',[{'id':0,'name':'x','type':'ellipse'}]),
    ('name',' x'),('threshold',2),('author_contact',' person@example.test'),
    ('author_contact','person\n@example.test'),('polygon',{'min_distance_px':-1}),
    ('polygon',{'spacing_percent':101}),('polygon',{'min_area_px':float('inf')}),
])
def test_manifest_invalid(field,value):
    d=json.loads((ROOT/'examples/segmentation/manifest.json').read_text());d[field]=value
    with pytest.raises(ValidationError): Manifest.model_validate(d)

def test_manifest_uniqueness_and_strictness(manifest):
    d=manifest.model_dump();d['labels'].append(d['labels'][0])
    with pytest.raises(ValidationError): Manifest.model_validate(d)
    with pytest.raises(ValidationError): InvokeRequest(image='x',threshold='0.5')

def test_legacy_manifest_normalized_without_exposing_threshold():
    manifest=Manifest(name='old',weights=['model.onnx'],labels=[{'id':0,'name':'region','type':'mask'}],threshold=.8)
    assert manifest.labels[0].type=='polygon'
    assert 'threshold' not in manifest.model_dump()
    assert not hasattr(manifest,'threshold')

def test_metadata_contract(manifest):
    mid='a'*20;rev='b'*16
    m=function_metadata(mid,rev,manifest.model_dump(),'nuclio')
    assert parse_function_id(m['metadata']['name'])==(mid,rev)
    assert m['metadata']['annotations']['type']=='detector'
    assert json.loads(m['metadata']['annotations']['spec'])[0]['type']=='rectangle'
    assert m['status']['httpPort']==0
    legacy={'name':'old','weights':['model.onnx'],'labels':[{'id':0,'name':'region','type':'mask'}],'threshold':.5}
    assert json.loads(function_metadata(mid,rev,legacy,'nuclio')['metadata']['annotations']['spec'])[0]['type']=='polygon'

def test_invalid_function_id():
    with pytest.raises(ValueError): parse_function_id('mr-../../password')

def test_image_contract(sample):
    a=decode_image(sample);assert a.shape==(48,64,3);assert a.dtype==np.uint8
    for bad in ['', 'data:image/png;base64,'+sample,'%%%%']:
        with pytest.raises((ValueError,OSError)): decode_image(bad)

def test_gif_rejected():
    f=io.BytesIO();Image.new('RGB',(2,2)).save(f,format='GIF')
    with pytest.raises(ValueError):decode_image(base64.b64encode(f.getvalue()).decode())


def test_legacy_mixed_manifest_keeps_function_metadata_and_shape_dispatch():
    legacy={'name':'mixed','weights':['model.onnx'],'labels':[
        {'id':0,'name':'region','type':'mask'},
        {'id':8,'name':'object','type':'rectangle'},
    ],'threshold':.5}
    manifest=Manifest.model_validate(legacy)
    meta=function_metadata('a'*20,'b'*16,legacy,'nuclio')
    labels=json.loads(meta['metadata']['annotations']['spec'])
    assert [label['type'] for label in labels]==['polygon','rectangle']
    output=encode_results([Mask(0,.1,np.ones((10,10),np.uint8)),Box(8,.1,(0,0,10,10))],manifest,(10,10,3))
    assert [item['type'] for item in output]==['polygon','rectangle']
    validate_wire(output,manifest,10,10)
    # Even when its first label is polygon, a tensor cannot encode rectangle labels.
    with pytest.raises(ValueError,match='require polygon labels'):
        encode_results(np.ones((2,10,10),np.uint8),manifest,(10,10,3))


def test_stored_legacy_mixed_revision_enumerates_and_describes(ctx):
    service=ctx['service'];mid='a'*20;rev='b'*16
    legacy={'name':'old mixed','weights':['model.onnx'],'labels':[
        {'id':0,'name':'region','type':'mask'},
        {'id':8,'name':'object','type':'rectangle'},
    ],'threshold':.8}
    service.store.create_model(mid,'alice')
    service.store.commit_revision(mid,rev,None,legacy,'digest','unused')
    functions=service.functions()
    metadata=functions[function_id(mid,rev)]
    assert [x['type'] for x in json.loads(metadata['metadata']['annotations']['spec'])]==['polygon','rectangle']
    detail=service.describe(mid)
    assert detail['manifest']['author_contact']==''
    for description in [detail['manifest'],detail['revisions'][0]['manifest']]:
        assert 'threshold' not in description
        assert description['labels'][0]['type']=='polygon'
        assert description['polygon']['min_distance_px']==2.
    # Serving the new contract does not rewrite the audited original manifest.
    assert service.store.revision(mid,rev)['manifest']==legacy
