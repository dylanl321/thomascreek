# Thomas Creek cloud media worker (see worker.py and server/hub/cloudmedia.py).
FROM mwader/static-ffmpeg:7.1 AS ffmpeg

FROM python:3.12-slim
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ladder.py worker.py ./
ENV PYTHONUNBUFFERED=1 STATE_DIR=/data WORK_DIR=/tmp/tc-media PORT=8080
EXPOSE 8080
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4); sys.exit(0)"
CMD ["python", "worker.py"]
