# SPDX-License-Identifier: MIT
import json
from types import SimpleNamespace
import numpy as np
import pytest
from geometry import mask_shape, polygon_mask, polygon_shape, point_array
from image_model import ImageInteractor
from protocol import ProtocolError, body_dict, decode_image, serve


def decode_rle(shape, size):
    full=np.zeros((size[1],size[0]),dtype=bool)
    if shape is None: return full
    *runs,x0,y0,x1,y1=shape['points']
    flat=np.concatenate([np.full(count,bool(i%2)) for i,count in enumerate(runs)])
    full[y0:y1+1,x0:x1+1]=flat.reshape(y1-y0+1,x1-x0+1)
    return full

@pytest.mark.parametrize('kind',['zero','one','full','holes','islands','random'])
def test_rle_roundtrip(kind):
    mask=np.zeros((20,24),dtype=bool)
    if kind=='one': mask[9,11]=True
    elif kind=='full': mask[:]=True
    elif kind=='holes': mask[2:17,3:22]=True; mask[5:9,6:10]=False
    elif kind=='islands': mask[1:5,1:6]=True; mask[13:18,17:22]=True
    elif kind=='random': mask=np.random.default_rng(1).random((20,24))>.5
    shape=mask_shape(mask)
    np.testing.assert_array_equal(mask,decode_rle(shape,(24,20)))
    if shape is not None:
        assert shape['type']=='mask' and shape['attributes']==[]
        assert all(type(x) is int for x in shape['points'])

@pytest.mark.parametrize('bad',[[-1,2],[24,2],[2,20],[float('nan'),1],[float('inf'),1]])
def test_invalid_points(bad):
    with pytest.raises(ProtocolError): point_array([bad],'points',(24,20))

@pytest.mark.parametrize('bad',[{},'hi',[[1]],[[1,2,3]],[[1,2],[3]],[[None,2]]])
def test_invalid_point_arrays(bad):
    with pytest.raises(ProtocolError): point_array(bad,'points',(24,20))

def test_optional_points_and_edge_box():
    assert point_array(None,'points',(24,20)).shape==(0,2)
    assert point_array([[24,20]],'box',(24,20),edge=True).tolist()==[[24,20]]

class Predictor:
    def __init__(self): self.set_calls=0; self.calls=[]
    def set_image(self,image): self.set_calls+=1; self.size=image.shape[:2]
    def predict(self,**kw):
        self.calls.append(kw)
        masks=np.zeros((3,*self.size),dtype=bool)
        masks[0,1:3,1:3]=1; masks[1,3:10,4:14]=1; masks[2,6:8,6:8]=1
        return masks,np.array([.2,.9,.5]),None

def test_points_labels_best_mask_and_cache(image_body):
    pred=Predictor(); interactor=ImageInteractor(pred)
    body={**image_body(),'pos_points':[[5,5]],'neg_points':[[0,0]],'obj_bbox':None}
    output=interactor(body)
    assert output['shapes'][0]['points'][-4:]==[4,3,13,9]
    np.testing.assert_array_equal(pred.calls[0]['point_labels'],[1,0])
    np.testing.assert_array_equal(pred.calls[0]['point_coords'],[[5,5],[0,0]])
    interactor({**body,'pos_points':[[6,6]]})
    assert pred.set_calls==1
    interactor({**body,**image_body(91)})
    assert pred.set_calls==2
    assert 'mask_input' not in pred.calls[1]  # no prompt state leaks across users

def test_box_only(image_body):
    pred=Predictor(); model=ImageInteractor(pred)
    model({**image_body(),'obj_bbox':[[1,1],[24,20]],'pos_points':[],'neg_points':[]})
    assert pred.calls[0]['point_coords'] is None
    np.testing.assert_array_equal(pred.calls[0]['box'],[1,1,24,20])

@pytest.mark.parametrize('extra',[{}, {'neg_points':[[1,1]]}, {'obj_bbox':[[5,5],[4,7]]}, {'obj_bbox':[[5,5]]}])
def test_reject_bad_prompts(image_body,extra):
    with pytest.raises(ProtocolError): ImageInteractor(Predictor())({**image_body(),**extra})

def test_empty_image_mask(image_body):
    pred=Predictor()
    pred.predict=lambda **kw:(np.zeros((1,20,24)),np.array([.9]),None)
    assert ImageInteractor(pred)({**image_body(),'pos_points':[[2,2]]})=={'shapes':[]}

def test_no_nan_scores(image_body):
    pred=Predictor(); pred.predict=lambda **kw:(np.zeros((1,20,24)),np.array([np.nan]),None)
    with pytest.raises(RuntimeError): ImageInteractor(pred)({**image_body(),'pos_points':[[2,2]]})

def test_polygon_geometry(polygon):
    mask=polygon_mask(polygon,(24,20)); shape=polygon_shape(mask)
    assert mask.sum()>100 and shape['type']=='polygon'
    assert polygon_shape(np.zeros((20,24))) is None
    assert polygon_shape(np.eye(3)) is None

@pytest.mark.parametrize('shape',[None,{'type':'rectangle','points':[1,1,2,2]}, {'type':'polygon','points':[1,1,2,2,3,3]}, {'type':'polygon','points':[1,1,30,2,3,3]}])
def test_invalid_polygons(shape):
    with pytest.raises(ProtocolError): polygon_mask(shape,(24,20))

@pytest.mark.parametrize('body',[b'{',None,[],3,'[]'])
def test_invalid_json(body):
    with pytest.raises(ProtocolError): body_dict(body)

@pytest.mark.parametrize('body',[{}, {'image':'@@@@'}, {'image':'aGVsbG8='}, {'image':33}])
def test_invalid_images(body):
    with pytest.raises(ProtocolError): decode_image(body)

def test_image_decode_and_json_forms(image_body):
    body=image_body()
    assert decode_image(body).mode=='RGB'
    assert body_dict(json.dumps(body).encode())==body
    assert body_dict(body) is body

def test_response_status_and_safe_error():
    logs=[]
    context=SimpleNamespace(Response=lambda **kw:kw,logger=SimpleNamespace(error=logs.append))
    ok=serve(context,SimpleNamespace(body='{}'),lambda data:{'shapes':[]})
    assert ok['status_code']==200 and json.loads(ok['body'])=={'shapes':[]}
    def fail(data): raise ProtocolError('capacity',429)
    assert serve(context,SimpleNamespace(body={}),fail)['status_code']==429
    def internal(data): raise RuntimeError('secret token not for output')
    error=serve(context,SimpleNamespace(body={}),internal)
    assert error['status_code']==500
    assert 'secret' not in error['body'] and all('secret' not in log for log in logs)


def test_fractional_points_inside_last_pixel():
    value=point_array([[23.75,19.75]],'points',(24,20))
    np.testing.assert_array_equal(value,[[23.75,19.75]])

def test_invalid_predicted_mask_dimensions(image_body):
    pred=Predictor(); pred.predict=lambda **kw:(np.zeros((1,10,10)),np.array([.9]),None)
    with pytest.raises(RuntimeError,match='dimensions'):
        ImageInteractor(pred)({**image_body(),'pos_points':[[2,2]]})
