# SPDX-License-Identifier: MIT
from enum import IntEnum
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip("torch")
from ultrasam_prompt_adapter import encode_points, restore_mask


class Index(IntEnum):
    NON_INIT_MASK_EMBED = 0
    NEG = 1
    POS = 2
    BOX_CORNER_A = 3
    BOX_CORNER_B = 4
    NOT_A_POINT = 5
    MASK_OUT = 6
    MASK_OUT_1 = 7
    MASK_OUT_2 = 8
    MASK_OUT_3 = 9
    IOU_OUT = 10


class EncoderSpy:
    n_output_tokens = 5
    image_embedding_size = (2, 3)

    def __init__(self):
        self.label_encoder = SimpleNamespace(
            label_embedding=SimpleNamespace(
                weight=torch.arange(44, dtype=torch.float32).reshape(11, 4)
            )
        )
        self.observed = None

    def process_prompt(self, padding, attention, points, labels, dense):
        self.observed = (padding, attention, points, labels, dense)
        return padding, attention, points, labels, torch.ones((*labels.shape, 4)), dense


def test_prompt_labels_use_upstream_indices_and_output_token_order():
    encoder = EncoderSpy()
    points = torch.tensor([[10.5, 20.5], [30.5, 40.5], [50.5, 60.5]])
    encoded = encode_points(encoder, points, torch.tensor([1, 1, 0]), Index)
    torch.testing.assert_close(encoded["padded_points"][0, 0, :3], points)
    assert encoded["padded_labels"].tolist() == [[[2, 2, 1, 5, 6, 7, 8, 9, 10]]]
    assert tuple(encoded["dense_embed"].shape) == (1, 1, 4, 2, 3)
    torch.testing.assert_close(
        encoded["dense_embed"][0, 0, :, 0, 0], torch.arange(4, dtype=torch.float32)
    )
    assert not encoded["prompt_padding_masks"].any()


def test_single_point_uses_seven_queries_and_distinct_padding_token():
    encoded = encode_points(
        EncoderSpy(), torch.tensor([[2.5, 4.5]]), torch.tensor([1]), Index
    )
    assert encoded["padded_labels"].tolist() == [[[2, 5, 6, 7, 8, 9, 10]]]


def test_invalid_binary_labels_rejected():
    with pytest.raises(ValueError, match="binary"):
        encode_points(
            EncoderSpy(), torch.tensor([[2.5, 4.5]]), torch.tensor([2]), Index
        )


def test_resize_uses_logits_before_thresholding():
    logits = torch.tensor([[-10.0, 1.0], [-10.0, 1.0]])
    actual = restore_mask(logits, (2, 2), (2, 2), (2, 6))
    expected = torch.tensor([[False, False, False, False, True, True]]).expand(2, 6)
    torch.testing.assert_close(actual, expected)


@pytest.mark.parametrize("original", [(9, 17), (17, 9)])
def test_padding_is_cropped_before_restoring_original_size(original):
    logits = torch.full((8, 8), -1.0)
    logits[4:, :] = 100.0
    actual = restore_mask(logits, (8, 8), (4, 8), original)
    assert tuple(actual.shape) == original
    assert not actual.any()


def test_transform_points_uses_actual_resize_ratios_and_pixel_center_offset():
    from ultrasam_model import UltraSamPredictor

    predictor = object.__new__(UltraSamPredictor)
    predictor.device = torch.device("cpu")
    predictor.metadata = {"scale_factor": (2.0, 1.5)}
    transformed = predictor.transform_points(np.array([[5, 6], [0, 0]], np.float32))
    torch.testing.assert_close(transformed, torch.tensor([[10.5, 9.5], [0.5, 0.5]]))
