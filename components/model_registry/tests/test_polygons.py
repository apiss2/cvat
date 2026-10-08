import numpy as np
import pytest

from registry.codec import encode_results, is_simple_polygon, polygon_area, _sample_contour
from registry.schema import Manifest, PolygonSettings
from registry.sdk import Mask
from registry.wire import validate_wire


def manifest(**options):
    return Manifest(name='segmentation', weights=['model.onnx'],
                    labels=[{'id':27,'name':'region','type':'polygon'}],
                    polygon={'min_distance_px':0.,'spacing_percent':0.,'min_area_px':0.,**options})


def ring(obj):
    return np.asarray(obj['points']).reshape(-1,2)


def test_binary_array_external_contour_hole_is_filled():
    bitmap=np.zeros((1,40,50),dtype=bool);bitmap[0,5:35,8:42]=True;bitmap[0,12:22,16:26]=False
    m=manifest()
    result=encode_results(bitmap,m,(40,50,3))
    assert len(result)==1 and result[0]['type']=='polygon'
    assert 'mask' not in result[0] and result[0]['confidence']==1.
    assert polygon_area(ring(result[0]))==33*29
    assert validate_wire(result,m,50,40)==result


def test_label_order_and_disconnected_regions():
    m=manifest()
    data=m.model_dump();data['labels']=[{'id':27,'name':'first','type':'polygon'},{'id':2,'name':'second','type':'polygon'}]
    m=Manifest.model_validate(data)
    bitmap=np.zeros((2,40,50),np.uint8)
    bitmap[0,2:10,2:10]=1;bitmap[0,20:30,20:30]=1;bitmap[1,10:20,30:40]=1
    output=encode_results(bitmap,m,(40,50,3))
    assert [x['label'] for x in output]==['first','first','second']


def test_four_connected_diagonal_regions_remain_separate():
    bitmap=np.zeros((1,20,20),np.uint8);bitmap[0,2:8,2:8]=1;bitmap[0,8:14,8:14]=1
    assert len(encode_results(bitmap,manifest(),(20,20,3)))==2


def test_minimum_area_filters_foreground_pixels_before_filled_outer_area():
    bitmap=np.zeros((1,40,40),np.uint8);bitmap[0,5:35,5:35]=1;bitmap[0,6:34,6:34]=0
    assert bitmap.sum()==116
    assert encode_results(bitmap,manifest(min_area_px=120.),(40,40,3))==[]


def test_minimum_area_also_filters_resulting_polygon_area():
    bitmap=np.zeros((1,10,10),np.uint8);bitmap[0,2:6,2:6]=1
    assert bitmap.sum()==16  # Pixel-center contour area is 9.
    assert encode_results(bitmap,manifest(min_area_px=10.),(10,10,3))==[]


def test_degenerate_single_pixel_line_and_empty_output_are_omitted():
    bitmap=np.zeros((1,20,20),np.uint8);bitmap[0,2,2]=1;bitmap[0,10,4:15]=1
    assert encode_results(bitmap,manifest(),(20,20,3))==[]
    assert encode_results(np.zeros_like(bitmap),manifest(),(20,20,3))==[]


@pytest.mark.parametrize('minimum,percent',[(0.,10.),(3.,0.),(6.,2.),(12.,5.)])
def test_closed_polygon_has_minimum_chord_distance(minimum,percent):
    bitmap=np.zeros((1,60,60),np.uint8);bitmap[0,5:46,5:46]=1
    m=manifest(min_distance_px=minimum,spacing_percent=percent)
    output=encode_results(bitmap,m,(60,60,3))
    assert len(output)==1
    points=ring(output[0]);distance=np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1)
    assert np.all(distance>=minimum-1e-9),distance
    assert validate_wire(output,m,60,60)==output
    if minimum==0.:
        assert len(points)==10  # 10 percent of the 160-pixel external perimeter.


def test_impossible_spacing_omits_component_instead_of_breaking_minimum():
    bitmap=np.ones((1,20,20),np.uint8)
    assert encode_results(bitmap,manifest(min_distance_px=100.),(20,20,3))==[]
    assert encode_results(bitmap,manifest(spacing_percent=50.),(20,20,3))==[]


def test_closing_edge_is_checked_and_pruned():
    contour=np.asarray([[0,0],[0,20],[20,20],[20,0],[1,0]],np.int32)
    m=PolygonSettings(min_distance_px=3.,spacing_percent=0.,min_area_px=0.)
    points=_sample_contour(contour,m)
    assert points is not None
    assert np.min(np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1))>=3.-1e-9


@pytest.mark.parametrize('bitmap',[
    np.ones((1,10,10),np.float32),np.full((1,10,10),255,np.uint8),
    np.zeros((10,10),bool),np.zeros((1,9,10),bool),np.zeros((2,10,10),bool),
    np.full((1,10,10),-1,np.int8),np.full((1,10,10),float('nan')),
])
def test_nonbinary_or_wrong_shape_segmentation_is_rejected(bitmap):
    with pytest.raises(ValueError):encode_results(bitmap,manifest(),(10,10,3))


def test_legacy_mask_adapter_uses_polygon_and_never_filters_score():
    output=encode_results([Mask(27,0.,np.ones((10,10),np.uint8))],manifest(),(10,10,3))
    assert len(output)==1 and output[0]['type']=='polygon' and output[0]['confidence']==0.
    validate_wire(output,manifest(),10,10)


@pytest.mark.parametrize('points',[
    [[0,0],[10,10],[0,10],[10,0]],  # Bow tie.
    [[0,0],[10,0],[10,10],[5,0],[0,10]],  # Nonadjacent edge touch.
    [[0,0],[10,0],[10,10],[0,0],[0,10]],  # Repeated vertex.
])
def test_self_intersections_rejected(points):
    points=np.asarray(points,np.float64)
    assert not is_simple_polygon(points)
    item={'label':'region','type':'polygon','confidence':1.,'points':points.ravel().tolist()}
    with pytest.raises(ValueError):validate_wire([item],manifest(),20,20)


@pytest.mark.parametrize('points',[
    [[0,0],[10,0],[10,10],[0,10]],
    [[0,0],[10,0],[10,10],[5,5],[0,10]],
])
def test_simple_convex_and_concave_polygons_accepted(points):
    points=np.asarray(points,np.float64)
    assert is_simple_polygon(points)
    item={'label':'region','type':'polygon','confidence':1.,'points':points.ravel().tolist()}
    validate_wire([item],manifest(),20,20)


@pytest.mark.parametrize('coordinates',[
    [0,0,10,0,10,10,0,1], # Short closing edge.
    [0,0,20,0,20,10,0,10], # Beyond rightmost image pixel center.
    [0,0,10,0,10], # Odd number of coordinates.
    [0,0,True,0,10,10],
    [0,0,float('nan'),0,10,10],
])
def test_wire_rejects_invalid_polygon(coordinates):
    m=manifest(min_distance_px=2.)
    item={'label':'region','type':'polygon','confidence':1.,'points':coordinates}
    with pytest.raises(ValueError):validate_wire([item],m,20,20)


def test_vertex_budget_requires_more_spacing():
    bitmap=np.ones((1,20,20),np.uint8)
    with pytest.raises(ValueError,match='4096 vertices'):
        encode_results(bitmap,manifest(spacing_percent=.001),(20,20,3))
