FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=America/New_York \
    BWI_DB_PATH=/data/bwi_monitor.db BWI_LOG_DIR=/data/logs BWI_WEB_HOST=0.0.0.0 BWI_WEB_PORT=8765
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bwi_monitor ./bwi_monitor
RUN useradd --create-home --uid 10001 bwi && mkdir -p /data && chown bwi /data
USER bwi
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
CMD ["python", "-m", "bwi_monitor"]
