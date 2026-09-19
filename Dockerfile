# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — build the Rust numeric core.
# If this stage fails the wheel is simply absent and the app falls back to the
# NumPy implementation, so the deploy degrades in performance, not availability.
# ---------------------------------------------------------------------------
# Built on the same Python base as the runtime. The crate targets abi3-py311 so the
# resulting wheel is interpreter-independent, but keeping the bases aligned removes
# the whole class of "wheel silently not installed" failures.
FROM python:3.12-slim-bookworm AS rust-builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain 1.82.0 --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
RUN pip install --no-cache-dir "maturin>=1.7,<2.0"
COPY core/ /build/core/
RUN cd core && maturin build --release --out /build/wheels || \
    (echo "WARNING: Rust core build failed; the NumPy fallback will be used." && \
     mkdir -p /build/wheels)

# ---------------------------------------------------------------------------
# Stage 2 — build the documentation website.
# ---------------------------------------------------------------------------
FROM node:22-slim AS web-builder
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# `npm ci` installs exactly the lockfile, so the deployed bundle matches what was tested.
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 3 — runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/app \
    PYTHONPATH=/app/api \
    TALAIA_DATA_DIR=/data \
    TALAIA_WEB_DIST=/app/web/dist
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install \
      "fastapi>=0.115" "uvicorn[standard]>=0.32" "httpx>=0.28" "duckdb>=1.1" \
      "shapely>=2.0" "pydantic>=2.9" "pydantic-settings>=2.6" "numpy>=1.26" \
      "orjson>=3.10" "rapidfuzz>=3.10" "openpyxl" "pandas"

# Install the Rust wheel when stage 1 produced one.
COPY --from=rust-builder /build/wheels/ /tmp/wheels/
RUN if ls /tmp/wheels/*.whl >/dev/null 2>&1; then \
        pip install /tmp/wheels/*.whl && echo "Rust core installed"; \
    else \
        echo "No Rust wheel; using the NumPy fallback"; \
    fi && rm -rf /tmp/wheels

# Bake the DuckDB spatial extension into the image so the first request does not
# depend on reaching extensions.duckdb.org at runtime.
RUN python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL spatial; INSTALL json;'); c.close()" \
    && echo "DuckDB extensions cached in \$HOME/.duckdb"

COPY api/ /app/api/
COPY sql/ /app/sql/
COPY --from=web-builder /web/dist /app/web/dist

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8000}/health || exit 1

CMD ["sh", "-c", "uvicorn talaia.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-keep-alive 75"]
