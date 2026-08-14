FROM python:3.13-alpine AS builder
# RUN apt-get update && apt-get install -y git
RUN apk add --no-cache git
RUN pip install GitPython
WORKDIR /build
COPY ytdlp_web_player/ /build/
RUN python src/version.py


FROM python:3.13-alpine

RUN apk add --no-cache ffmpeg deno
WORKDIR /app

# Install our local yt-dlp fork (with the extra search/category prefixes)
# before other deps so requirements.txt (which no longer lists yt-dlp) can't
# clobber it. `curl-cffi` is required for the browser impersonation those
# extractors need (the site answers 410 without it).
COPY yt-dlp/ /opt/yt-dlp/
RUN pip install --no-cache-dir "/opt/yt-dlp[default,curl-cffi]"

COPY ytdlp_web_player/src/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=builder /build/version.txt /app/
COPY ytdlp_web_player/src/. /app
COPY ytdlp_web_player/API_DOCS.md /app/API_DOCS.md
COPY ytdlp_web_player/VERSION /app/VERSION
COPY ytdlp_web_player/extension/extension.js /app/static/extension.js
EXPOSE 5000
ENV FLASK_APP=main.py
CMD ["python3", "main.py"]