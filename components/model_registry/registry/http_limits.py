# SPDX-License-Identifier: MIT
from __future__ import annotations
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(status_code=413, detail="Request body is too large")


class GuardMiddleware:
    """Authenticate before multipart parsing; enforce limits even for chunked requests."""
    def __init__(self, app, auth=None, max_bytes: int = 64 * 1024**2):
        self.app, self.auth, self.max_bytes = app, auth, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope["path"]
        # ASGI mounts / --root-path retain the external prefix in scope.path.
        # Apply the same effective route path as Starlette before authentication.
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path):]
        limit = (2 * 1024**3 + 2 * 1024**2) if path in ("/api/models", "/api/upload") and request.method == "POST" else self.max_bytes
        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                headers.extend([(b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")])
                message["headers"] = headers
            await send(message)

        try:
            if self.auth:
                if path.startswith("/internal/"):
                    self.auth.service(request)
                elif path in ("/api/auth/login", "/api/auth/logout", "/api/auth/register"):
                    # Registry-specific credential entry and sessions are retired.
                    raise HTTPException(404, "Use CVAT to sign in, sign out or create an account")
                elif path.startswith("/api/"):
                    scope.setdefault("state", {})["user"] = await self.auth.user(request)
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                raise HTTPException(413, "Request body is too large")
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, guarded_send)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise BodyTooLarge()
            return message

        try:
            return await self.app(scope, bounded_receive, guarded_send)
        except BodyTooLarge:
            if not started:
                return await JSONResponse({"detail": "Request body is too large"}, status_code=413)(scope, receive, guarded_send)
            raise
