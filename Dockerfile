# Build stage: resolve and install dependencies with uv into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime stage: slim image, non-root user, only the venv, app code and committed results.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 tenancy
WORKDIR /app
COPY --from=build --chown=tenancy:tenancy /app /app
COPY --chown=tenancy:tenancy results ./results
# The app writes tenant databases, backups and exports under /app/data at runtime.
RUN mkdir -p /app/data && chown tenancy:tenancy /app/data
USER tenancy
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8875 \
    TENANCYLAB_DATA=/app/data TENANCYLAB_RESULTS=/app/results
EXPOSE 8875
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8875/healthz', timeout=2)"
CMD ["tenancylab", "serve"]
