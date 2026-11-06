# SPDX-License-Identifier: MIT
"""Real encapsulated video codecs and sparse, multi-GiB fragment boundaries."""

import io
import struct

import numpy as np
import pytest

av = pytest.importorskip("av")
pydicom = pytest.importorskip("pydicom")
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import SecondaryCaptureImageStorage

from components.dicom_import import DicomImportError
from components.dicom_import.video import (
    FragmentedVideoReader,
    is_video_transfer_syntax,
    iter_video_frames,
)

UID_PREFIX = "1.2.840.10008.1.2.4."
DELIMITER = struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)


def ITEM(length):
    return struct.pack("<HHI", 0xFFFE, 0xE000, length)


def encoded_video(codec="libx264", container="mp4", *, bits=8, monochrome=False):
    buffer = io.BytesIO()
    # A very short transport stream otherwise has too few sync packets for
    # FFmpeg's automatic format probing, even when its payload is valid.
    options = {"muxrate": "1000000"} if container == "mpegts" else {}
    with av.open(buffer, mode="w", format=container, options=options) as output:
        stream = output.add_stream(codec, rate=25)
        stream.width, stream.height = 32, 24
        stream.pix_fmt = "yuv420p10le" if bits == 10 else "yuv420p"
        stream.thread_count = 1
        if codec == "libx264":
            stream.options = {"preset": "ultrafast", "crf": "1"}
        elif codec == "libx265":
            stream.options = {
                "preset": "ultrafast",
                "crf": "1",
                "x265-params": "log-level=error:pools=1:frame-threads=1",
            }
        for level in (40, 100, 200):
            image = np.full((24, 32, 3), level, dtype=np.uint8)
            if not monochrome:
                image[:, :, 1:] //= 2
            if bits == 10:
                image = image.astype(np.uint16) * 257
            frame = av.VideoFrame.from_ndarray(image, format="rgb48le" if bits == 10 else "rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return buffer.getvalue()


def video_dicom(path, payload, *, syntax="102", fragments=1, bits=8, monochrome=False):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = UID_PREFIX + syntax
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = "1.2.301.1"
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID = "SYNTHETIC"
    ds.StudyDate = "20260102"
    ds.StudyTime = "030405"
    ds.SeriesNumber = 2
    ds.InstanceNumber = 1
    ds.StudyInstanceUID = "1.2.300"
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SamplesPerPixel = 1 if monochrome else 3
    ds.PhotometricInterpretation = "MONOCHROME2" if monochrome else "YBR_PARTIAL_420"
    if not monochrome:
        ds.PlanarConfiguration = 0
    ds.Rows, ds.Columns = 24, 32
    ds.NumberOfFrames = 3
    ds.FrameTime = 40
    ds.BitsAllocated = 16 if bits == 10 else 8
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 0
    ds.PixelData = encapsulate([payload], fragments_per_frame=fragments, has_bot=False)
    ds[0x7FE00010].is_undefined_length = True
    ds.save_as(path, enforce_file_format=True)
    header = pydicom.dcmread(path, defer_size=1)
    offset = header.get_item(0x7FE00010, keep_deferred=True).value_tell
    return path, header, offset


def decode(source, *, max_frame_bytes=1024**3):
    return list(iter_video_frames(*source, max_frame_bytes=max_frame_bytes))


@pytest.mark.parametrize(
    "syntax,codec,container,bits,fragments",
    [
        ("100", "mpeg2video", "mpegts", 8, 1),
        ("100.1", "mpeg2video", "mpegts", 8, 3),
        ("102", "libx264", "mp4", 8, 1),
        ("102.1", "libx264", "mp4", 8, 3),
        ("104.1", "libx264", "mpegts", 8, 3),
        ("107", "libx265", "mp4", 8, 3),
        ("108", "libx265", "mp4", 10, 3),
    ],
)
def test_real_video_codec_frames(tmp_path, syntax, codec, container, bits, fragments):
    source = video_dicom(
        tmp_path / "video.dcm",
        encoded_video(codec, container, bits=bits),
        syntax=syntax,
        fragments=fragments,
        bits=bits,
    )
    frames = decode(source)
    assert len(frames) == 3
    assert all(frame.shape == (24, 32, 3) for frame in frames)
    assert all(frame.dtype == (np.uint16 if bits == 10 else np.uint8) for frame in frames)
    assert all(frame[:, :, 0].mean() > frame[:, :, 1].mean() for frame in frames)
    assert frames[0].mean() < frames[1].mean() < frames[2].mean()
    if bits == 10:
        assert 255 < int(frames[-1].max()) <= 1023


def test_mpeg2_monochrome_uses_one_sample(tmp_path):
    source = video_dicom(
        tmp_path / "monochrome.dcm",
        encoded_video("mpeg2video", "mpegts", monochrome=True),
        syntax="100",
        monochrome=True,
    )
    frames = decode(source)
    assert len(frames) == 3
    assert all(frame.shape == (24, 32) and frame.dtype == np.uint8 for frame in frames)
    assert frames[0].mean() < frames[-1].mean()


@pytest.mark.parametrize("number", range(100, 107))
def test_fragmentable_video_transfer_syntaxes_are_recognized(number):
    assert is_video_transfer_syntax(UID_PREFIX + str(number))
    assert is_video_transfer_syntax(UID_PREFIX + str(number) + ".1")
    assert not is_video_transfer_syntax("1.2.840.10008.1.2.5")


@pytest.mark.parametrize("syntax", ["105", "105.1", "106", "106.1"])
def test_stereoscopic_video_is_not_silently_flattened(tmp_path, syntax):
    source = video_dicom(tmp_path / "stereo.dcm", encoded_video(), syntax=syntax)
    with pytest.raises(DicomImportError, match="Stereoscopic"):
        decode(source)


@pytest.mark.parametrize("frames", [2, 4])
def test_video_frame_count_must_match_dicom_metadata(tmp_path, frames):
    source = video_dicom(tmp_path / "count.dcm", encoded_video())
    source[1].NumberOfFrames = frames
    with pytest.raises(DicomImportError, match="frames|frame count"):
        decode(source)


def test_video_dimensions_must_match_dicom_metadata(tmp_path):
    source = video_dicom(tmp_path / "size.dcm", encoded_video())
    source[1].Rows = 26
    with pytest.raises(DicomImportError, match="dimensions differ"):
        decode(source)


def test_video_transfer_syntax_must_match_actual_codec(tmp_path):
    source = video_dicom(
        tmp_path / "codec.dcm", encoded_video("mpeg2video", "mpegts"), syntax="102"
    )
    with pytest.raises(DicomImportError, match="match the video codec"):
        decode(source)


def test_video_bit_depth_must_match_actual_codec(tmp_path):
    source = video_dicom(
        tmp_path / "bits.dcm", encoded_video("libx265", bits=10), syntax="107", bits=8
    )
    with pytest.raises(DicomImportError, match="bit depth differs"):
        decode(source)


def test_frame_budget_is_checked_before_opening_the_decoder(tmp_path, monkeypatch):
    source = video_dicom(tmp_path / "budget.dcm", encoded_video())
    monkeypatch.setattr(av, "open", lambda *_args, **_kwargs: pytest.fail("Decoder was opened"))
    with pytest.raises(DicomImportError, match="MAX_FRAME_BYTES"):
        decode(source, max_frame_bytes=24 * 32 * 3 - 1)


def test_video_payload_cannot_be_a_playlist(tmp_path):
    source = video_dicom(
        tmp_path / "playlist.dcm",
        b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://invalid.test/video.ts\n",
    )
    with pytest.raises(DicomImportError, match="video decoding failed"):
        decode(source)


def test_fragment_reader_seeks_across_real_sparse_four_gib_boundary(tmp_path):
    path = tmp_path / "large-fragments.dcm"
    pixel_offset = 2**32 + 64
    first_length = 2**32 - 2
    with path.open("wb") as output:
        output.seek(pixel_offset)
        output.write(ITEM(0))
        output.write(ITEM(first_length))
        output.seek(first_length - 2, io.SEEK_CUR)
        output.write(b"AB")
        output.write(ITEM(6) + b"cdefgh" + DELIMITER)
    reads = []

    class AuditedSource:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size):
            assert 0 <= size <= 1024**2
            reads.append(size)
            return self.stream.read(size)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    with path.open("rb") as source:
        with FragmentedVideoReader(AuditedSource(source), pixel_offset, fragmentable=True) as view:
            assert sum(reads) == 32  # Only item headers, no payload scan or copy.
            assert view.seek(0, io.SEEK_END) == first_length + 6 > 2**32
            view.seek(first_length - 2)
            assert view.read(6) == b"ABcdef"
            assert view.seek(-2, io.SEEK_END) == first_length + 4
            assert view.read(99) == b"gh"
            assert view.read(1) == b""
            view.seek(0)
            assert len(view.read(2**34)) == 1024**2
            with pytest.raises(DicomImportError, match="Unbounded"):
                view.read()
    assert path.stat().st_size > 8 * 1024**3


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        DELIMITER,
        ITEM(2) + b"xx",
        ITEM(4) + b"\1\0\0\0" + ITEM(2) + b"xx" + DELIMITER,
        ITEM(0) + ITEM(0) + DELIMITER,
        ITEM(0) + ITEM(3) + b"abc" + DELIMITER,
        ITEM(0) + ITEM(0xFFFFFFFF),
        ITEM(0) + ITEM(2) + b"xx",
        ITEM(0) + ITEM(100) + b"xx" + DELIMITER,
        ITEM(0) + ITEM(2) + b"xx" + struct.pack("<HHI", 0xFFFE, 0xE0DD, 2),
    ],
)
def test_invalid_fragment_structure_is_rejected(payload):
    with pytest.raises(DicomImportError):
        FragmentedVideoReader(io.BytesIO(payload), 0, fragmentable=True)


def test_non_fragmentable_syntax_rejects_multiple_fragments():
    payload = ITEM(0) + ITEM(2) + b"ab" + ITEM(2) + b"cd" + DELIMITER
    with pytest.raises(DicomImportError, match="one fragment"):
        FragmentedVideoReader(io.BytesIO(payload), 0, fragmentable=False)


def test_video_reader_closes_without_closing_the_callers_source():
    source = io.BytesIO(ITEM(0) + ITEM(2) + b"ab" + DELIMITER)
    with FragmentedVideoReader(source, 0, fragmentable=False) as view:
        assert view.read(2) == b"ab"
    assert not source.closed
    with pytest.raises(ValueError, match="closed"):
        view.read(1)


def test_prepare_registers_encapsulated_video_as_one_video_task(tmp_path):
    from components.dicom_import.conversion import prepare

    source = video_dicom(tmp_path / "video.dcm", encoded_video(), syntax="102.1", fragments=3)
    plan = prepare([source[0]], tmp_path / "converted", "TEST")
    assert not plan.failed_tasks
    assert len(plan.tasks) == 1
    task = plan.tasks[0]
    assert task.video and task.frames == 3
    assert task.media[0].suffix == ".mp4"
    assert next(iter(task.tags.values()))["TransferSyntaxUID"] == UID_PREFIX + "102.1"
