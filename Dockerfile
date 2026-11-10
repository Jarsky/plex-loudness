# plex-loudness: one image for the controller and the worker (ROLE=controller | worker | both).
# docker-compose.yml runs it as plex-loudness (controller) and/or plex-loudness-worker.
FROM python:3.13-alpine

RUN apk add --no-cache ffmpeg docker-cli tini su-exec tzdata

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[tui]" && rm -rf /src
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

ENV WORK_DIR=/work MEDIA_ROOT=/media TERM=xterm-256color COLORTERM=truecolor
WORKDIR /work
ENTRYPOINT ["/sbin/tini", "--", "entrypoint.sh"]
CMD ["serve"]
