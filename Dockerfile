FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 reelsomet
WORKDIR /workspace
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY server/ server/
COPY data/fsm/ data/fsm/
COPY --from=web /web/dist/ web/dist/
RUN mkdir -p /workspace/var && chown -R reelsomet:reelsomet /workspace
USER reelsomet
ENV PYTHONUNBUFFERED=1 REELSOMET_HOST=0.0.0.0 TZ=UTC
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"
CMD ["python", "-m", "server.main", "--config", "/workspace/var/config.yaml", "--host", "0.0.0.0"]
