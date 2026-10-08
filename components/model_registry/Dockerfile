# SPDX-License-Identifier: MIT
FROM python:3.12.12-slim-bookworm AS gateway
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-control.txt ./
RUN pip install --no-cache-dir -r requirements-control.txt
COPY registry ./registry
USER 65532:65532
CMD ["python", "-m", "uvicorn", "registry.gateway:create_gateway", "--factory", "--host", "0.0.0.0", "--port", "8070", "--workers", "1", "--no-access-log"]

FROM gateway AS manager
# Only the management process needs access to the Docker socket and host bind directories.
USER root
COPY static ./static
ENV TMPDIR=/data/uploads
CMD ["python", "-m", "uvicorn", "registry.manager:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091", "--workers", "1", "--no-access-log"]
