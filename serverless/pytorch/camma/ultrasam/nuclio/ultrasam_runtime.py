# SPDX-License-Identifier: MIT
"""Check NVRTC without a GPU, and cuDNN with a GPU before serving requests.

Run probes in a child process so a native abort has a useful parent-side error.
This module intentionally imports no model or PyTorch code in the parent.
"""

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path


def check_nvrtc():
    """Load the exact unversioned name, then compile to exercise builtins."""
    try:
        library = ctypes.CDLL("libnvrtc.so")
    except OSError as exc:
        raise RuntimeError(
            "Cannot load libnvrtc.so. Rebuild the UltraSAM function image with "
            "cuda-nvrtc-11-8 and cuda-nvrtc-dev-11-8; restarting the old image "
            "does not install these libraries. Loader error: " + str(exc)
        ) from exc
    integer = ctypes.c_int
    pointer = ctypes.c_void_p
    string = ctypes.c_char_p
    signatures = {
        "nvrtcVersion": [ctypes.POINTER(integer), ctypes.POINTER(integer)],
        "nvrtcCreateProgram": [
            ctypes.POINTER(pointer),
            string,
            string,
            integer,
            ctypes.POINTER(string),
            ctypes.POINTER(string),
        ],
        "nvrtcCompileProgram": [pointer, integer, ctypes.POINTER(string)],
        "nvrtcGetProgramLogSize": [pointer, ctypes.POINTER(ctypes.c_size_t)],
        "nvrtcGetProgramLog": [pointer, pointer],
        "nvrtcDestroyProgram": [ctypes.POINTER(pointer)],
    }
    for name, args in signatures.items():
        function = getattr(library, name)
        function.argtypes = args
        function.restype = integer

    def require_success(status, operation):
        if status:
            raise RuntimeError(f"{operation} failed with NVRTC status {status}")

    major, minor = integer(), integer()
    require_success(
        library.nvrtcVersion(ctypes.byref(major), ctypes.byref(minor)), "nvrtcVersion"
    )
    if (major.value, minor.value) != (11, 8):
        raise RuntimeError(f"Expected NVRTC 11.8, got {major.value}.{minor.value}")
    program = pointer()
    source = b'extern "C" __global__ void probe(float *x) { x[0] = 1.0f; }'
    require_success(
        library.nvrtcCreateProgram(
            ctypes.byref(program), source, b"ultrasam_probe.cu", 0, None, None
        ),
        "nvrtcCreateProgram",
    )
    try:
        # PTX generation needs no CUDA driver or GPU. Ada (RTX 4090) is supported
        # by CUDA 11.8; compiling for it also detects an accidentally older NVRTC.
        options = (string * 1)(b"--gpu-architecture=compute_89")
        status = library.nvrtcCompileProgram(program, 1, options)
        if status:
            size = ctypes.c_size_t()
            require_success(
                library.nvrtcGetProgramLogSize(program, ctypes.byref(size)),
                "nvrtcGetProgramLogSize",
            )
            log = ctypes.create_string_buffer(max(size.value, 1))
            require_success(
                library.nvrtcGetProgramLog(program, log), "nvrtcGetProgramLog"
            )
            raise RuntimeError(
                f"nvrtcCompileProgram failed with NVRTC status {status}: "
                + log.value.decode("utf-8", errors="replace")
            )
    finally:
        library.nvrtcDestroyProgram(ctypes.byref(program))
    return {
        "nvrtc": "11.8",
        "nvrtc_library": "libnvrtc.so",
        "nvrtc_compile": True,
        "ptx_architecture": "compute_89",
    }


def check_cuda():
    """Exercise cuDNN convolution; device enumeration alone is insufficient."""
    import torch

    if torch.__version__ != "2.0.0+cu118" or torch.version.cuda != "11.8":
        raise RuntimeError("UltraSAM requires torch 2.0.0+cu118")
    if not torch.cuda.is_available():
        raise RuntimeError("No NVIDIA GPU is available to the UltraSAM worker")
    if not torch.backends.cudnn.is_available() or not torch.backends.cudnn.version():
        raise RuntimeError("cuDNN is unavailable to the UltraSAM worker")
    with torch.inference_mode(), torch.backends.cudnn.flags(enabled=True):
        inputs = torch.ones((1, 3, 32, 32), device="cuda")
        weights = torch.ones((8, 3, 3, 3), device="cuda")
        # Explicit cuDNN dispatch prevents a fallback from hiding a load failure.
        output = torch.ops.aten.cudnn_convolution.default(
            inputs, weights, [0, 0], [1, 1], [1, 1], 1, False, True, False
        )
        torch.cuda.synchronize()
        if tuple(output.shape) != (1, 8, 30, 30) or not torch.all(output == 27).item():
            raise RuntimeError("cuDNN convolution returned unexpected values")
    return {
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "gpu": torch.cuda.get_device_name(0),
        "cudnn_convolution": True,
    }


def run_probe(mode, timeout=60):
    if mode not in ("build", "cuda"):
        raise ValueError("Probe mode must be build or cuda")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--mode", mode, "--child"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"UltraSAM {mode} probe timed out after {timeout}s") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        reason = (
            f"signal {-result.returncode}"
            if result.returncode < 0
            else f"exit code {result.returncode}"
        )
        raise RuntimeError(f"UltraSAM {mode} probe failed ({reason}): {detail}")
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"UltraSAM {mode} probe returned invalid JSON") from exc
    if (
        not isinstance(report, dict)
        or report.get("nvrtc_compile") is not True
        or (mode == "cuda" and report.get("cudnn_convolution") is not True)
    ):
        raise RuntimeError(f"UltraSAM {mode} probe did not confirm required operations")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("build", "cuda"), default="cuda")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        # Do not leave a core dump if a native CUDA/cuDNN library aborts.
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        report = check_nvrtc()
        if args.mode == "cuda":
            report.update(check_cuda())
    else:
        report = run_probe(args.mode)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
