# SPDX-License-Identifier: MIT
import base64
import io
import sys
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'nuclio'))

@pytest.fixture
def image_body():
    def encode(value=90, size=(24, 20)):
        image = Image.new('RGB', size, (value, value, value))
        stream=io.BytesIO(); image.save(stream, format='PNG')
        return {'image':base64.b64encode(stream.getvalue()).decode()}
    return encode

@pytest.fixture
def polygon():
    return {'type':'polygon', 'points':[3.,3.,16.,3.,16.,15.,3.,15.]}
