FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG BUILD_VERSION=development
ARG BUILD_REVISION=unknown
RUN python -c 'import json,sys; json.dump(dict(version=sys.argv[1], revision=sys.argv[2], timezone="Asia/Shanghai"), open("/app/build-info.json", "w"))' "$BUILD_VERSION" "$BUILD_REVISION"
FROM base AS controller
USER root
RUN apt-get update && apt-get install -y --no-install-recommends adb \
    && rm -rf /var/lib/apt/lists/*
COPY controller ./controller
COPY phone ./phone
COPY scripts/preflight.py ./scripts/preflight.py
COPY deploy/adb/adb-client.sh ./deploy/adb/adb-client.sh
RUN chmod 755 /app/deploy/adb/adb-client.sh
USER 1000:1000
CMD ["python", "controller/file_sync.py"]

FROM base AS dashboard
COPY dashboard/app.py dashboard/performance.py ./
COPY dashboard/static ./static
COPY controller/live_config.py controller/configuration.py controller/target_guard.py controller/config_service.py ./
USER 1000:1000
CMD ["gunicorn", "--bind", "127.0.0.1:8027", "--workers", "2", "--threads", "4", "--timeout", "30", "app:app"]
