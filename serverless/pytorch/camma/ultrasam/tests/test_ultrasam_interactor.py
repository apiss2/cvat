# SPDX-License-Identifier: MIT
import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import numpy as np
import pytest
from PIL import Image
from ultrasam_interactor import ImageInteractor
from ultrasam_protocol import ProtocolError


def request(color=80, points=None, negative=None, size=(17, 11)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (color, color, color)).save(buffer, format="PNG")
    return dict(
        image=base64.b64encode(buffer.getvalue()).decode(),
        pos_points=points if points is not None else [[3, 4]],
        neg_points=negative if negative is not None else [],
    )


class Predictor:
    def __init__(self):
        self.images = []
        self.prompts = []
        self.shape = None
        self.fail_image = False

    def set_image(self, image):
        self.images.append(image.copy())
        if self.fail_image:
            raise RuntimeError("embedding failed")
        self.shape = image.shape[:2]

    def predict(self, points, labels):
        self.prompts.append((points.copy(), labels.copy()))
        mask = np.zeros(self.shape, dtype=bool)
        mask[1:5, 2:6] = True
        return mask, 0.9


def test_multiple_positive_negative_points_and_single_image_cache():
    predictor = Predictor()
    operation = ImageInteractor(predictor)
    first = operation(request(points=[[3, 4], [4, 5]], negative=[[8, 9]]))
    operation(request(points=[[7, 4]], negative=[[2, 6], [8, 1]]))
    assert first["shapes"][0]["type"] == "mask"
    assert len(predictor.images) == 1
    np.testing.assert_array_equal(predictor.prompts[0][1], [1, 1, 0])
    np.testing.assert_array_equal(predictor.prompts[1][0], [[7, 4], [2, 6], [8, 1]])
    np.testing.assert_array_equal(predictor.prompts[1][1], [1, 0, 0])
    operation(request(color=81))
    assert len(predictor.images) == 2


def test_shape_is_included_in_cache_identity():
    predictor = Predictor()
    operation = ImageInteractor(predictor)
    operation(request(size=(12, 6)))
    operation(request(size=(6, 12)))
    assert len(predictor.images) == 2


def test_failed_set_image_invalidates_cache():
    predictor = Predictor()
    operation = ImageInteractor(predictor)
    operation(request())
    predictor.fail_image = True
    with pytest.raises(RuntimeError):
        operation(request(color=81))
    predictor.fail_image = False
    operation(request())
    assert len(predictor.images) == 3


def test_no_positive_and_total_point_limit():
    operation = ImageInteractor(Predictor())
    with pytest.raises(ProtocolError, match="positive"):
        operation(request(points=[], negative=[[3, 4]]))
    with pytest.raises(ProtocolError, match="total"):
        operation(request(points=[[3, 4]] * 128, negative=[[4, 4]]))


@pytest.mark.parametrize("bbox", [None, []])
def test_cvat_empty_box_field_is_accepted(bbox):
    data = request()
    data["obj_bbox"] = bbox
    assert ImageInteractor(Predictor())(data)["shapes"]


def test_box_is_not_silently_ignored():
    data = request()
    data["obj_bbox"] = [[0, 0], [5, 5]]
    with pytest.raises(ProtocolError, match="point prompts only"):
        ImageInteractor(Predictor())(data)


def test_empty_mask_and_invalid_predictor_output():
    predictor = Predictor()
    predictor.predict = lambda *args: (np.zeros((11, 17), dtype=bool), 0.5)
    assert ImageInteractor(predictor)(request()) == {"shapes": []}
    predictor.predict = lambda *args: (np.zeros((17, 11), dtype=bool), 0.5)
    with pytest.raises(RuntimeError, match="dimensions"):
        ImageInteractor(predictor)(request())


def test_concurrent_requests_do_not_mix_image_embeddings():
    class SlowPredictor(Predictor):
        def set_image(self, image):
            super().set_image(image)
            self.color = image[0, 0, 0]

        def predict(self, points, labels):
            before = self.color
            time.sleep(0.01)
            assert before == self.color
            return super().predict(points, labels)

    operation = ImageInteractor(SlowPredictor())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(operation, [request(color=20 + i) for i in range(8)]))
    assert all(result["shapes"] for result in results)
