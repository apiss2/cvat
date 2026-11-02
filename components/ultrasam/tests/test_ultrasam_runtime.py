# SPDX-License-Identifier: MIT
"""Exercise startup failures without requiring a GPU or model checkpoint."""

import importlib.util
from contextlib import contextmanager
import json
import os
import signal
import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import ultrasam_runtime


@pytest.fixture
def cuda_contract(monkeypatch):
    """Exercise the probe's decisions without a CUDA driver or native libraries."""
    state = SimpleNamespace(
        gpu_available=True, cudnn_available=True, cudnn_version=8700,
        output=np.full((1, 8, 30, 30), 27, dtype=np.float32), failure=None,
    )
    events = []
    module = ModuleType("torch")
    module.__version__ = "2.0.0+cu118"
    module.version = SimpleNamespace(cuda="11.8")

    @contextmanager
    def inference_mode():
        events.append("inference_enter")
        try:
            yield
        finally:
            events.append("inference_exit")

    @contextmanager
    def flags(*, enabled):
        assert enabled is True
        events.append("cudnn_enter")
        try:
            yield
        finally:
            events.append("cudnn_exit")

    def ones(shape, *, device):
        assert device == "cuda"
        events.append(("allocate", shape))
        return np.ones(shape, dtype=np.float32)

    def convolve(inputs, weights, padding, stride, dilation, groups, benchmark, deterministic, allow_tf32):
        assert inputs.shape == (1, 3, 32, 32) and weights.shape == (8, 3, 3, 3)
        assert np.all(inputs == 1) and np.all(weights == 1)
        assert (padding, stride, dilation, groups) == ([0, 0], [1, 1], [1, 1], 1)
        assert (benchmark, deterministic, allow_tf32) == (False, True, False)
        events.append("cudnn_convolution")
        if state.failure == "convolution":
            raise RuntimeError("native convolution failure")
        return state.output

    def synchronize():
        events.append("synchronize")
        if state.failure == "synchronize":
            raise RuntimeError("native synchronization failure")

    def device_name(index):
        assert index == 0
        events.append("device_name")
        return "Visible test GPU"

    module.inference_mode, module.ones, module.all = inference_mode, ones, np.all
    module.cuda = SimpleNamespace(
        is_available=lambda: state.gpu_available,
        synchronize=synchronize, get_device_name=device_name,
    )
    module.backends = SimpleNamespace(cudnn=SimpleNamespace(
        is_available=lambda: state.cudnn_available,
        version=lambda: state.cudnn_version, flags=flags,
    ))
    module.ops = SimpleNamespace(aten=SimpleNamespace(cudnn_convolution=SimpleNamespace(default=convolve)))
    monkeypatch.setitem(sys.modules, "torch", module)
    return module, state, events


def test_cuda_probe_uses_explicit_cudnn_dispatch_and_checks_output(cuda_contract):
    _, _, events = cuda_contract
    assert ultrasam_runtime.check_cuda() == {
        "torch": "2.0.0+cu118", "cuda": "11.8", "cudnn": 8700,
        "gpu": "Visible test GPU", "cudnn_convolution": True,
    }
    assert events == [
        "inference_enter", "cudnn_enter", ("allocate", (1, 3, 32, 32)),
        ("allocate", (8, 3, 3, 3)), "cudnn_convolution", "synchronize",
        "cudnn_exit", "inference_exit", "device_name",
    ]


@pytest.mark.parametrize("condition,message", [
    ("torch_version", "requires torch"), ("cuda_version", "requires torch"),
    ("gpu_available", "No NVIDIA GPU"), ("cudnn_available", "cuDNN is unavailable"),
    ("cudnn_version", "cuDNN is unavailable"),
])
def test_cuda_probe_rejects_unsupported_runtime_before_allocation(cuda_contract, condition, message):
    module, state, events = cuda_contract
    if condition == "torch_version":
        module.__version__ = "2.6.0+cpu"
    elif condition == "cuda_version":
        module.version.cuda = "12.4"
    else:
        setattr(state, condition, False if condition != "cudnn_version" else None)
    with pytest.raises(RuntimeError, match=message):
        ultrasam_runtime.check_cuda()
    assert events == []


@pytest.mark.parametrize("output", [
    np.full((1, 8, 31, 30), 27, dtype=np.float32),
    np.full((1, 8, 30, 30), 26, dtype=np.float32),
    np.full((1, 8, 30, 30), np.nan, dtype=np.float32),
])
def test_cuda_probe_never_reports_success_for_invalid_output(cuda_contract, output):
    _, state, events = cuda_contract
    state.output = output
    with pytest.raises(RuntimeError, match="unexpected values"):
        ultrasam_runtime.check_cuda()
    assert "synchronize" in events
    assert events[-2:] == ["cudnn_exit", "inference_exit"]
    assert "device_name" not in events


@pytest.mark.parametrize("failure", ["convolution", "synchronize"])
def test_cuda_probe_preserves_native_errors_and_exits_contexts(cuda_contract, failure):
    _, state, events = cuda_contract
    state.failure = failure
    with pytest.raises(RuntimeError, match="native .* failure"):
        ultrasam_runtime.check_cuda()
    assert events[-2:] == ["cudnn_exit", "inference_exit"]
    assert "device_name" not in events


@pytest.fixture
def child_program(monkeypatch, tmp_path):
    """Use a real child process, including actual timeout and abort behavior."""

    def install(source):
        script = tmp_path / "probe_child.py"
        script.write_text(textwrap.dedent(source), encoding="utf-8")
        monkeypatch.setattr(ultrasam_runtime, "__file__", str(script))

    return install


@pytest.mark.parametrize("mode", ["build", "cuda"])
def test_probe_returns_report_from_successful_child(child_program, mode):
    report = {"nvrtc_compile": True, "nvrtc": "11.8"}
    if mode == "cuda":
        report.update(cudnn_convolution=True, gpu="test GPU")
    child_program(f"""
        import sys
        assert sys.argv[1:] == ["--mode", {mode!r}, "--child"]
        print({json.dumps(report)!r})
    """)
    assert ultrasam_runtime.run_probe(mode) == report


def test_probe_reports_child_exception_and_exit_status(child_program):
    child_program('raise RuntimeError("libnvrtc.so is unavailable")')
    with pytest.raises(RuntimeError) as error:
        ultrasam_runtime.run_probe("cuda")
    assert "exit code 1" in str(error.value)
    assert "libnvrtc.so is unavailable" in str(error.value)


def test_probe_times_out_instead_of_waiting_indefinitely(child_program):
    child_program("import time; time.sleep(10)")
    with pytest.raises(RuntimeError, match="build probe timed out after 0.1s"):
        ultrasam_runtime.run_probe("build", timeout=0.1)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal return codes")
def test_native_abort_is_reported_without_aborting_parent(child_program):
    child_program("""
        import os
        import resource
        import sys
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        print("cuDNN could not load libnvrtc.so", file=sys.stderr, flush=True)
        os.abort()
    """)
    with pytest.raises(RuntimeError) as error:
        ultrasam_runtime.run_probe("cuda")
    assert f"signal {signal.SIGABRT}" in str(error.value)
    assert "cuDNN could not load libnvrtc.so" in str(error.value)


@pytest.mark.parametrize(
    "mode,report",
    [
        ("build", []),
        ("build", {}),
        ("build", {"nvrtc_compile": 1}),
        ("cuda", {"nvrtc_compile": True}),
        ("cuda", {"nvrtc_compile": True, "cudnn_convolution": False}),
        ("cuda", {"nvrtc_compile": False, "cudnn_convolution": True}),
    ],
)
def test_probe_rejects_report_without_required_successes(child_program, mode, report):
    child_program(f"print({json.dumps(report)!r})")
    with pytest.raises(RuntimeError, match="did not confirm required operations"):
        ultrasam_runtime.run_probe(mode)


def test_probe_rejects_invalid_json(child_program):
    child_program('print("not a JSON report")')
    with pytest.raises(RuntimeError, match="returned invalid JSON"):
        ultrasam_runtime.run_probe("build")


@pytest.fixture
def model_module():
    pytest.importorskip("torch")
    import ultrasam_model

    return ultrasam_model


class WarmupPredictor:
    def __init__(self, device="cpu", fail_at=None, invalid_output=None):
        self.device = SimpleNamespace(type=device)
        self.fail_at = fail_at
        self.invalid_output = invalid_output
        self.failure = RuntimeError(f"failure during {fail_at}")
        self.features = object()
        self.metadata = {"previous_image": True}
        self.calls = []

    def set_image(self, image):
        self.calls.append("set_image")
        assert image.ndim == 3 and image.shape[2] == 3
        assert image.dtype == np.uint8
        self.image = image
        self.features = object()
        self.metadata = {"warmup_image": True}
        if self.fail_at == "set_image":
            raise self.failure

    def predict(self, points, labels):
        self.calls.append("predict")
        assert points.shape == (len(labels), 2)
        assert set(labels.tolist()) == {0, 1}
        assert np.isfinite(points).all()
        if self.fail_at == "predict":
            raise self.failure
        mask_shape = (
            (1, 1) if self.invalid_output == "mask_shape" else self.image.shape[:2]
        )
        score = float("nan") if self.invalid_output == "score" else 0.8
        return np.zeros(mask_shape, dtype=bool), score


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_warmup_exercises_prompts_and_clears_synthetic_image(
    model_module, monkeypatch, device
):
    predictor = WarmupPredictor(device=device)

    def synchronize(actual_device):
        assert actual_device is predictor.device
        assert predictor.features is not None and predictor.metadata is not None
        predictor.calls.append("synchronize")

    monkeypatch.setattr(model_module.torch.cuda, "synchronize", synchronize)
    model_module.warmup_predictor(predictor)
    expected = ["set_image", "predict"]
    if device == "cuda":
        expected.append("synchronize")
    assert predictor.calls == expected
    assert predictor.features is None
    assert predictor.metadata is None


@pytest.mark.parametrize("fail_at", ["set_image", "predict", "synchronize"])
def test_warmup_clears_features_and_metadata_on_exception(
    model_module, monkeypatch, fail_at
):
    predictor = WarmupPredictor(device="cuda", fail_at=fail_at)

    def synchronize(_device):
        raise predictor.failure

    monkeypatch.setattr(model_module.torch.cuda, "synchronize", synchronize)
    with pytest.raises(RuntimeError) as error:
        model_module.warmup_predictor(predictor)
    assert error.value is predictor.failure
    assert predictor.features is None
    assert predictor.metadata is None


@pytest.mark.parametrize("invalid_output", ["mask_shape", "score"])
def test_warmup_rejects_invalid_prediction_and_clears_cache(
    model_module, invalid_output
):
    predictor = WarmupPredictor(invalid_output=invalid_output)
    with pytest.raises(RuntimeError, match="invalid mask or score"):
        model_module.warmup_predictor(predictor)
    assert predictor.features is None
    assert predictor.metadata is None


@pytest.mark.parametrize("fail_at", [None, "probe", "warmup"])
def test_init_publishes_operation_only_after_probe_and_warmup(monkeypatch, fail_at):
    source = Path(__file__).resolve().parents[3] / "serverless/pytorch/camma/ultrasam/nuclio/main.py"
    spec = importlib.util.spec_from_file_location("ultrasam_startup_test", source)
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    messages = []
    context = SimpleNamespace(
        user_data=SimpleNamespace(), logger=SimpleNamespace(info=messages.append)
    )
    calls = []
    model, predictor, operation = object(), object(), object()
    failure = RuntimeError(f"startup failure in {fail_at}")

    def stage(name):
        assert not hasattr(context.user_data, "operation")
        calls.append(name)
        if name == fail_at:
            raise failure

    def probe(mode):
        assert mode == "cuda"
        stage("probe")
        return {"nvrtc_compile": True, "cudnn_convolution": True}

    def build_model(*, device):
        assert device == "cuda"
        stage("build_model")
        return model

    def make_predictor(actual_model):
        assert actual_model is model
        stage("predictor")
        return predictor

    def warmup(actual_predictor):
        assert actual_predictor is predictor
        stage("warmup")

    def interactor(actual_predictor):
        assert actual_predictor is predictor
        stage("interactor")
        return operation

    model_stub = ModuleType("ultrasam_model")
    model_stub.build_model = build_model
    model_stub.UltraSamPredictor = make_predictor
    model_stub.warmup_predictor = warmup
    interactor_stub = ModuleType("ultrasam_interactor")
    interactor_stub.ImageInteractor = interactor
    monkeypatch.setattr(ultrasam_runtime, "run_probe", probe)
    monkeypatch.setitem(sys.modules, "ultrasam_model", model_stub)
    monkeypatch.setitem(sys.modules, "ultrasam_interactor", interactor_stub)

    if fail_at is None:
        main.init_context(context)
        assert calls == ["probe", "build_model", "predictor", "warmup", "interactor"]
        assert context.user_data.operation is operation
    else:
        with pytest.raises(RuntimeError) as error:
            main.init_context(context)
        assert error.value is failure
        assert not hasattr(context.user_data, "operation")
        expected = (
            ["probe"]
            if fail_at == "probe"
            else ["probe", "build_model", "predictor", "warmup"]
        )
        assert calls == expected
