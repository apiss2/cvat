import numpy as np
import pytest

from registry.codec import (
    _outer_pixel_contour, _polygons, _rings_conflict, _sample_contour, _segment_distance, encode_results,
    is_simple_polygon, polygon_area,
)
from registry.schema import Manifest, PolygonSettings
from registry.sdk import Mask
from registry.wire import validate_wire


def manifest(**options):
    return Manifest(name='segmentation', weights=['model.onnx'],
                    labels=[{'id':27,'name':'region','type':'polygon'}],
                    polygon={'min_distance_px':0.,'max_error_px':0.,'min_area_px':0.,**options})


def ring(obj):
    return np.asarray(obj['points']).reshape(-1,2)


def test_binary_array_external_contour_hole_is_filled():
    bitmap=np.zeros((1,40,50),dtype=bool);bitmap[0,5:35,8:42]=True;bitmap[0,12:22,16:26]=False
    m=manifest()
    result=encode_results(bitmap,m,(40,50,3))
    assert len(result)==1 and result[0]['type']=='polygon'
    assert 'mask' not in result[0] and result[0]['confidence']==1.
    assert polygon_area(ring(result[0]))==34*30
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
    output=encode_results(bitmap,manifest(),(20,20,3))
    assert len(output)==2
    first,second=map(ring,output)
    assert not _rings_conflict(first,first,second,second)


def test_minimum_area_filters_foreground_pixels_before_filled_outer_area():
    bitmap=np.zeros((1,40,40),np.uint8);bitmap[0,5:35,5:35]=1;bitmap[0,6:34,6:34]=0
    assert bitmap.sum()==116
    assert encode_results(bitmap,manifest(min_area_px=120.),(40,40,3))==[]


def test_minimum_area_also_filters_resulting_polygon_area():
    contour=np.array([[2,2],[2,5],[5,5],[5,2]])
    assert _sample_contour(contour,PolygonSettings(min_distance_px=0.,max_error_px=0.,min_area_px=10.)) is None


def test_single_pixel_line_and_image_edges_have_positive_area():
    bitmap=np.zeros((1,20,20),np.uint8);bitmap[0,0,0]=1;bitmap[0,19,4:20]=1
    m=manifest()
    output=encode_results(bitmap,m,(20,20,3))
    assert sorted(polygon_area(ring(item)) for item in output)==[1.,16.]
    assert validate_wire(output,m,20,20)==output
    assert encode_results(np.zeros_like(bitmap),m,(20,20,3))==[]


@pytest.mark.parametrize('minimum,error',[(0.,0.),(3.,0.),(6.,2.),(12.,5.)])
def test_rectangle_needs_only_four_vertices(minimum,error):
    bitmap=np.zeros((1,60,60),np.uint8);bitmap[0,5:46,5:46]=1
    m=manifest(min_distance_px=minimum,max_error_px=error)
    output=encode_results(bitmap,m,(60,60,3))
    assert len(output)==1
    points=ring(output[0]);distance=np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1)
    assert len(points)==4
    assert np.all(distance>=minimum-1e-9)
    assert validate_wire(output,m,60,60)==output


def test_impossible_spacing_omits_component_instead_of_breaking_minimum():
    bitmap=np.ones((1,20,20),np.uint8)
    assert encode_results(bitmap,manifest(min_distance_px=100.),(20,20,3))==[]


def test_closing_edge_is_checked_and_pruned():
    contour=np.asarray([[0,0],[0,20],[20,20],[20,0],[1,0]],np.int32)
    m=PolygonSettings(min_distance_px=3.,max_error_px=0.,min_area_px=0.)
    points=_sample_contour(contour,m)
    assert points is not None
    assert len(points)==4
    assert np.min(np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1))>=3.-1e-9


def test_sharp_feature_requires_error_budget_before_removal():
    contour=np.asarray([[0,0],[0,20],[20,20],[20,2],[18,0],[2,0]],np.int32)
    assert _sample_contour(contour, PolygonSettings(min_distance_px=3.,max_error_px=0.,min_area_px=0.)) is None
    points = _sample_contour(contour, PolygonSettings(min_distance_px=3.,max_error_px=2.,min_area_px=0.))
    assert points is not None
    assert len(points) == 4


def test_start_on_a_straight_run_adds_no_vertex():
    contour=np.asarray([[5,0],[10,0],[10,10],[0,10],[0,0]],np.int32)
    points=_sample_contour(contour,PolygonSettings(min_distance_px=0.,max_error_px=0.,min_area_px=0.))
    assert sorted(points.tolist())==[[0.,0.],[0.,10.],[10.,0.],[10.,10.]]


@pytest.mark.parametrize('length',[2,3,10])
def test_one_pixel_wide_whisker_is_retained_with_exact_settings(length):
    plain=np.zeros((1,40,40),np.uint8);plain[0,15:30,10:30]=1
    plain[0,15-length:15,20]=1
    output=encode_results(plain,manifest(),(40,40,3))
    assert len(output)==1
    assert polygon_area(ring(output[0]))==plain.sum()
    assert validate_wire(output,manifest(),40,40)==output


def test_one_pixel_wide_bridge_preserves_one_component():
    import cv2

    m=manifest(min_distance_px=2.,max_error_px=1.)
    bitmap=np.zeros((1,40,60),np.uint8);bitmap[0,10:30,5:25]=1;bitmap[0,10:30,35:55]=1;bitmap[0,20,25:35]=1
    output=encode_results(bitmap,m,(40,60,3))
    assert len(output)==1
    points=ring(output[0])
    assert polygon_area(points)==810.
    assert is_simple_polygon(points)
    assert np.min(np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1))>=2.
    for y,x in np.argwhere(bitmap[0]):
        assert cv2.pointPolygonTest(points.astype(np.float32),(float(x)+.5,float(y)+.5),False)>0
    assert validate_wire(output,m,60,40)==output
    bitmap[0,20,25:35]=0
    assert len(encode_results(bitmap,m,(40,60,3)))==2


def test_diagonal_saddle_in_one_component_has_simple_exterior():
    bitmap=np.ones((1,3,3),np.uint8);bitmap[0,0,0]=0;bitmap[0,1,1]=0
    output=encode_results(bitmap,manifest(),(3,3,3))
    assert len(output)==1 and is_simple_polygon(ring(output[0]))


def test_approximation_conflict_is_not_confused_with_distance_between_opposite_edges():
    a=np.array([[0.,0.],[3.,0.],[3.,3.],[0.,3.]])
    b=np.array([[4.,0.],[7.,0.],[7.,3.],[4.,3.]])
    assert not _rings_conflict(a,a,b,b)
    extended=b.copy();extended[[0,3],0]=2.
    assert _rings_conflict(a,a,extended,b)


def test_narrow_tip_is_rerouted_within_both_limits():
    # The two tip pixels are closer than the minimum distance and neither can
    # be removed within the error limit; other contour points must be used.
    m=manifest(min_distance_px=2.,max_error_px=1.)
    bitmap=np.zeros((1,30,30),np.uint8);bitmap[0,2:12,5:20]=1;bitmap[0,12:20,10:12]=1;bitmap[0,20,10]=1
    output=encode_results(bitmap,m,(30,30,3))
    assert len(output)==1
    points=ring(output[0])
    assert points[:,1].max()==21.
    assert np.min(np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1))>=2.-1e-9
    assert validate_wire(output,m,30,30)==output


# A narrow tail ending in a single pixel. Rerouting the short edge at its tip
# within 1 px used to cross the far side of the tail and drop the whole region.
NARROW_TAIL=[
    '..###........',
    '.#####.......',
    '.#####.......',
    '########.....',
    '#########....',
    '#########....',
    '##########...',
    '###########..',
    '.###########.',
    '.###########.',
    '.###########.',
    '..###########',
    '...##########',
    '...##########',
    '.....########',
    '.......######',
    '.........####',
    '..........###',
    '...........##',
    '...........##',
    '...........##',
    '...........##',
    '...........#.',
]


def test_rerouted_tip_does_not_cross_the_far_side():
    bitmap=np.pad(np.array([[c=='#' for c in row] for row in NARROW_TAIL],np.uint8),2)[None]
    m=manifest(min_distance_px=2.,max_error_px=1.,min_area_px=10.)
    output=encode_results(bitmap,m,bitmap.shape[1:]+(3,))
    assert len(output)==1
    assert validate_wire(output,m,bitmap.shape[2],bitmap.shape[1])==output


def model_like_mask(seed,size=256,grid=32):
    """Mask made the way segmentation models make one.

    A smooth low-resolution score map is upsampled bilinearly and thresholded
    at 0.5, so outlines are smooth curves quantized to pixels, with occasional
    narrow necks and tips where the score barely exceeds the threshold.
    """
    import cv2
    rng=np.random.default_rng(seed)
    score=cv2.GaussianBlur(rng.standard_normal((grid,grid)).astype(np.float32),(0,0),1.5)
    score=(score-score.mean())/score.std()
    return (cv2.resize(score,(size,size),interpolation=cv2.INTER_LINEAR)>.5).astype(np.uint8)


@pytest.mark.parametrize('seed',range(4))
def test_model_like_regions_are_converted_within_both_limits(seed):
    import cv2
    settings=PolygonSettings()
    count,components,stats,_=cv2.connectedComponentsWithStats(model_like_mask(seed),connectivity=4)
    regions=0
    for index in range(1,count):
        if stats[index][4]<100:
            continue
        regions+=1
        component=(components==index).astype(np.uint8)
        assert list(_polygons(component,settings)),'a region was dropped'
        loop=_outer_pixel_contour(component,0,0,component)
        points=_sample_contour(loop,settings)
        assert points is not None
        assert is_simple_polygon(points)
        edges=list(zip(points,np.roll(points,-1,axis=0)))
        assert min(np.linalg.norm(b-a) for a,b in edges)>=settings.min_distance_px-1e-9
        error=np.min([_segment_distance(loop,a,b) for a,b in edges],axis=0)
        assert error.max()<=settings.max_error_px+1e-9
    assert regions>=5


def test_legacy_polygon_settings_are_normalized():
    settings = PolygonSettings(min_distance_px=3., spacing_percent=2.5, min_area_px=7.)
    assert settings.model_dump() == {"min_distance_px": 3., "max_error_px": 2.5, "min_area_px": 7.}
    assert PolygonSettings(spacing_percent=0., max_error_px=1.).max_error_px == 1.
    with pytest.raises(ValueError):
        PolygonSettings(spacing_percent=-1.)


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
    [0,0,21,0,21,10,0,10], # Beyond right image edge.
    [0,0,10,0,10], # Odd number of coordinates.
    [0,0,True,0,10,10],
    [0,0,float('nan'),0,10,10],
])
def test_wire_rejects_invalid_polygon(coordinates):
    m=manifest(min_distance_px=2.)
    item={'label':'region','type':'polygon','confidence':1.,'points':coordinates}
    with pytest.raises(ValueError):validate_wire([item],m,20,20)


def test_vertex_budget_requires_more_approximation(monkeypatch):
    import registry.codec as codec
    monkeypatch.setattr(codec, 'MAX_POLYGON_VERTICES', 3)
    bitmap=np.ones((1,20,20),np.uint8)
    with pytest.raises(ValueError,match='4096 vertices'):
        encode_results(bitmap,manifest(),(20,20,3))
