# SPDX-License-Identifier: MIT
"""Run only in a no-network, non-root container with one read-only model package."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from .codec import decode_image, encode_results
from .schema import InvokeRequest, Manifest
from .sdk import ModelBase, ModelContext, PredictParams

log = logging.getLogger("model-worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def check_onnx_files(package: Path, manifest: Manifest) -> None:
    import onnx
    def check_tensor(tensor):
        if tensor.data_location == onnx.TensorProto.EXTERNAL or tensor.external_data:
            raise ValueError("ONNX external tensor data is not supported; export self-contained .onnx files")

    def walk_attribute(attribute):
        for tensor in attribute.tensors:
            check_tensor(tensor)
        if attribute.HasField("t"):
            check_tensor(attribute.t)
        if attribute.HasField("g"):
            walk_graph(attribute.g)
        for graph in attribute.graphs:
            walk_graph(graph)
        sparse = list(attribute.sparse_tensors)
        if attribute.HasField("sparse_tensor"):
            sparse.append(attribute.sparse_tensor)
        for tensor in sparse:
            check_tensor(tensor.values)
            check_tensor(tensor.indices)

    def walk_nodes(nodes):
        for node in nodes:
            for attribute in node.attribute:
                walk_attribute(attribute)

    def walk_graph(graph):
        for tensor in graph.initializer:
            check_tensor(tensor)
        for sparse in graph.sparse_initializer:
            check_tensor(sparse.values)
            check_tensor(sparse.indices)
        walk_nodes(graph.node)

    for filename in manifest.weights:
        model = onnx.load_model(package / filename, load_external_data=False)
        walk_graph(model.graph)
        for function in model.functions:
            walk_nodes(function.node)
            for attribute in function.attribute_proto:
                walk_attribute(attribute)
        for training in model.training_info:
            walk_graph(training.initialization)
            walk_graph(training.algorithm)
        onnx.checker.check_model(model)


def create_worker(package: Path = Path("/model")) -> FastAPI:
    state: dict = {}
    mutex = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        manifest = Manifest.model_validate_json((package / "manifest.json").read_bytes())
        check_onnx_files(package, manifest)
        # The control plane never executes this import.
        sys.path.insert(0, str(package))
        spec = importlib.util.spec_from_file_location("uploaded_model", package / "model.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot import model.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls = getattr(module, "Model", None)
        if not isinstance(cls, type) or not issubclass(cls, ModelBase):
            raise TypeError("model.py must declare class Model(ModelBase)")
        model = cls()
        providers = tuple(os.getenv("MR_PROVIDERS", "CPUExecutionProvider").split(","))
        model.load(ModelContext(package, manifest, providers))
        state.update(model=model, manifest=manifest)
        log.info("stage=load status=ready providers=%s", providers)
        try:
            yield
        finally:
            model.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"ready": "model" in state}

    @app.post("/predict")
    def predict(body: InvokeRequest, request: Request):
        request_id = request.headers.get("x-request-id", "unknown")[:80]
        if not mutex.acquire(blocking=False):
            raise HTTPException(503, "worker busy")
        try:
            image = decode_image(body.image)
            result = state["model"].predict(image, PredictParams(request_id))
            output = encode_results(result, state["manifest"], image.shape)
            log.info("request_id=%s stage=predict objects=%d", request_id, len(output))
            return output
        except Exception as exc:
            details = traceback.format_exc()[-16000:]
            log.error("request_id=%s stage=predict\n%s", request_id, details)
            raise HTTPException(422, {"request_id": request_id, "error": str(exc), "traceback": details}) from exc
        finally:
            mutex.release()

    return app


app = create_worker()
