FROM python:3.12-slim

# ffmpeg for cutting/rendering, espeak-ng only for the labeled demo, tini for clean shutdown.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY rolecall ./rolecall
COPY assets ./assets
RUN pip install --no-cache-dir . tzdata

ENV ROLECALL_DATA=/data \
    CAPTION_FONT=/app/assets/fonts/ArchivoBlack-Regular.ttf \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["rolecall", "serve", "--host", "0.0.0.0", "--port", "8080"]
