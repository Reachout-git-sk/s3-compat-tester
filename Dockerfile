FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY s3compat ./s3compat
# moto[server] is included so `--moto` works inside the container too
RUN pip install ".[dev]" && useradd --create-home runner

USER runner
WORKDIR /work
ENTRYPOINT ["s3compat"]
CMD ["--help"]
