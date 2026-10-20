FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN test -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf || (apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*)
WORKDIR /app
COPY --chown=1000:1000 app.py ./
COPY --chown=1000:1000 telegram.py ./
COPY --chown=1000:1000 video.py ./
COPY --chown=1000:1000 static ./static
ENV HOST=0.0.0.0 DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
CMD ["python", "app.py"]
