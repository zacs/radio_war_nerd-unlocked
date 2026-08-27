FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORK_DIR=/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rwnfeed ./rwnfeed

RUN useradd --create-home --uid 10001 rwn \
    && mkdir -p /data \
    && chown -R rwn:rwn /data /app
USER rwn

VOLUME ["/data"]

# tini reaps the scheduler cleanly on `docker stop`.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "rwnfeed"]
