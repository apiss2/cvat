# SPDX-License-Identifier: MIT
"""Only the three Nuclio APIs consumed by CVAT are exposed. Not a full dashboard."""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from .http_limits import GuardMiddleware
from .schema import InvokeRequest, parse_function_id

log = logging.getLogger("model-gateway")
MAX_RESPONSE = 128 * 1024**2


def safe_origin(value: str) -> str:
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
        raise ValueError("Gateway upstream must be an http(s) origin without credentials, path or query")
    return value.rstrip("/")


def create_gateway(*, registry_url=None, nuclio_url=None, token_file=None, namespace=None, registry_client=None, nuclio_client=None) -> FastAPI:
    registry_url = safe_origin(registry_url or os.getenv("MR_REGISTRY_URL", "http://model-registry:8091"))
    nuclio_url = safe_origin(nuclio_url or os.getenv("MR_NUCLIO_URL", "http://nuclio:8070"))
    namespace = namespace or os.getenv("MR_NAMESPACE", "nuclio")
    token_file = Path(token_file or os.getenv("MR_SERVICE_TOKEN_FILE", "/run/secrets/service_token"))
    ca_file = os.getenv("MR_CA_FILE", "")
    registry_verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
    registry = registry_client or httpx.Client(base_url=registry_url, timeout=httpx.Timeout(190, connect=5), verify=registry_verify, trust_env=False)
    legacy = nuclio_client or httpx.Client(base_url=nuclio_url, timeout=httpx.Timeout(300, connect=5), trust_env=False)

    @asynccontextmanager
    async def lifespan(app):
        yield
        registry.close()
        legacy.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GuardMiddleware)

    def header_check(request):
        if request.headers.get("x-nuclio-project-name", "cvat") != "cvat":
            raise HTTPException(400, "Only project cvat is supported")
        if request.headers.get("x-nuclio-function-namespace", namespace) != namespace:
            raise HTTPException(400, "Function namespace mismatch")

    def legacy_headers(request):
        return {
            "x-nuclio-project-name": "cvat", "x-nuclio-function-namespace": namespace,
            "x-nuclio-invoke-via": "domain-name", "x-nuclio-path": "/",
            "x-nuclio-invoke-timeout": request.headers.get("x-nuclio-invoke-timeout", "300s"),
        }

    def registry_headers(request_id=""):
        try:
            token = token_file.read_text().strip()
        except OSError as exc:
            raise HTTPException(503, "Gateway service-token file is unavailable") from exc
        if len(token) < 32:
            raise HTTPException(503, "Invalid gateway service-token configuration")
        return {"Authorization": "Bearer " + token, "X-Request-ID": request_id}

    def forward(client, method, path, headers, *, content=None, timeout=None):
        kwargs = {"headers": headers}
        if content is not None:
            kwargs["content"] = content
            headers["Content-Type"] = "application/json"
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            with client.stream(method, path, **kwargs) as response:
                output = bytearray()
                for chunk in response.iter_bytes():
                    if len(output) + len(chunk) > MAX_RESPONSE:
                        raise HTTPException(502, "Upstream response too large")
                    output.extend(chunk)
                return Response(bytes(output), status_code=response.status_code,
                    media_type=response.headers.get("content-type", "application/json").split(";")[0],
                    headers={"X-Request-ID": response.headers.get("x-request-id", headers.get("X-Request-ID", ""))})
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "Upstream timeout; inspect registry or Nuclio logs") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Cannot reach upstream service") from exc

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/functions")
    def functions(request: Request):
        header_check(request)
        merged = {}
        failed = []
        for name, client, path in (
            ("nuclio", legacy, "/api/functions"),
            ("registry", registry, "/internal/functions"),
        ):
            try:
                headers = legacy_headers(request) if name == "nuclio" else registry_headers()
                response = forward(client, "GET", path, headers, timeout=5)
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                data = json.loads(response.body)
                if not isinstance(data, dict):
                    raise ValueError("Invalid function catalog")
                source = {}
                for metadata in data.values():
                    identifier = metadata["metadata"]["name"]
                    if not isinstance(identifier, str):
                        raise ValueError("Invalid function identifier")
                    if name == "registry":
                        parse_function_id(identifier)
                    if name == "nuclio" and identifier.startswith("mr-"):
                        raise HTTPException(409, "The mr- function prefix is reserved for the model registry")
                    if identifier in merged or identifier in source:
                        raise HTTPException(409, "Function identifier collision")
                    source[identifier] = metadata
                merged.update(source)
            except HTTPException as exc:
                if exc.status_code == 409:
                    raise
                failed.append(name)
                log.warning("catalog_source=%s unavailable", name)
            except (ValueError, KeyError, TypeError, OSError):
                failed.append(name)
                log.warning("catalog_source=%s unavailable", name)
        if len(failed) == 2:
            raise HTTPException(502, "Both model catalog sources are unavailable")
        return Response(json.dumps(merged, ensure_ascii=False), media_type="application/json", headers={"X-Model-Catalog-Unavailable": ",".join(failed)})

    @app.get("/api/functions/{identifier}")
    def function(identifier: str, request: Request):
        header_check(request)
        if identifier.startswith("mr-"):
            try:
                parse_function_id(identifier)
            except ValueError as exc:
                raise HTTPException(404, "Unknown model function") from exc
            return forward(registry, "GET", "/internal/functions/" + identifier, registry_headers())
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", identifier):
            raise HTTPException(400, "Invalid function name")
        return forward(legacy, "GET", "/api/functions/" + quote(identifier, safe=""), legacy_headers(request))

    @app.post("/api/function_invocations")
    async def invoke(request: Request):
        header_check(request)
        identifier = request.headers.get("x-nuclio-function-name", "")
        raw = await request.body()
        # Network calls below run in the thread pool to avoid blocking other requests.
        from starlette.concurrency import run_in_threadpool
        if identifier.startswith("mr-"):
            try:
                parse_function_id(identifier)
                InvokeRequest.model_validate_json(raw)
            except ValueError as exc:
                raise HTTPException(422, "Invalid model function request") from exc
            return await run_in_threadpool(forward, registry, "POST", f"/internal/functions/{identifier}/invoke", registry_headers(uuid.uuid4().hex), content=raw)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", identifier):
            raise HTTPException(400, "Invalid function name")
        headers = legacy_headers(request)
        headers["x-nuclio-function-name"] = identifier
        # Preserve SAM2 prompts, tracking states and every original payload field verbatim.
        return await run_in_threadpool(forward, legacy, "POST", "/api/function_invocations", headers, content=raw)

    return app
