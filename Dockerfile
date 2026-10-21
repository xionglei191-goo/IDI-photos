FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS frontend
WORKDIR /build
COPY deploy/vps/frontend/package.json deploy/vps/frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS browser
WORKDIR /browser
COPY deploy/vps/browser/package.json deploy/vps/browser/package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89 AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    IDI_DATA_DIR=/var/lib/idi IDI_SECRET_KEY_FILE=/var/lib/idi-key/master.key \
    IDI_NETWORK_FILE=/run/idi/network.json IDI_FRONTEND_DIR=/opt/idi/frontend/dist \
    HOME=/var/lib/idi OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/opt/idi/browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libgomp1 ca-certificates tzdata fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 idi && useradd --uid 10001 --gid 10001 --no-create-home idi \
    && mkdir -p /var/lib/idi /var/lib/idi-key /run/idi \
    && chown 10001:10001 /var/lib/idi /var/lib/idi-key /run/idi \
    && chmod 700 /var/lib/idi /var/lib/idi-key /run/idi
WORKDIR /opt/idi
COPY deploy/vps/requirements.lock.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=browser /usr/local/bin/node /usr/local/bin/node
COPY --from=browser /browser/node_modules ./frontend/node_modules
RUN node frontend/node_modules/playwright/cli.js install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY idi/ ./idi/
COPY scripts/platform-login.mjs ./scripts/platform-login.mjs
COPY scripts/vps-browser-smoke.mjs ./scripts/vps-browser-smoke.mjs
RUN pip install --no-cache-dir --no-deps .
COPY --from=frontend /build/dist ./frontend/dist
USER 10001:10001
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/health',timeout=4)); assert r.get('application')=='IDI'"]
CMD ["python", "-m", "idi.vps"]
