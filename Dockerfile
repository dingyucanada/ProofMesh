FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 proofmesh \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin proofmesh

COPY requirements.txt requirements-production.txt requirements-production.lock pyproject.toml README.md ./
COPY src ./src
ARG PROOFMESH_INSTALL_PROFILE=development
RUN if [ "$PROOFMESH_INSTALL_PROFILE" = "production" ]; then \
      pip install --no-cache-dir --require-hashes -r requirements-production.lock; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi \
    && pip install --no-cache-dir --no-deps .

COPY data ./data
COPY config ./config
RUN mkdir -p /app/var /app/artifacts /app/config/trust \
    && chown -R proofmesh:proofmesh /app/var /app/artifacts /app/config/trust

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["uvicorn", "proofmesh.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
