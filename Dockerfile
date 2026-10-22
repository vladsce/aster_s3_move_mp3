ARG PUID=1000
ARG PGID=1000

FROM python:3.12-slim AS runtime

ARG PUID
ARG PGID
ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    PYTHONPATH=/app \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    HOME=/tmp \
    TZ=Europe/Chisinau \
    AWS_EC2_METADATA_DISABLED=true \
    AWS_REQUEST_CHECKSUM_CALCULATION=when_required \
    AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /data/records /var/log/archiver \
    && chown -R "${PUID}:${PGID}" /app /data/records /var/log/archiver \
    && chmod 1777 /var/log/archiver

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN chown -R "${PUID}:${PGID}" /app \
    && chmod -R a+rX /app

USER ${PUID}:${PGID}

CMD ["python", "-m", "app.main"]

FROM runtime AS test

ARG PUID
ARG PGID

USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
RUN chown -R "${PUID}:${PGID}" /app \
    && chmod -R a+rX /app
ENV PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest_cache" \
    MOTO_S3_CUSTOM_ENDPOINTS=https://s3.test.local
USER ${PUID}:${PGID}

CMD ["pytest", "-q"]
