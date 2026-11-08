# One image for both roles:
#   controller: docker run -it ... plex-loudness            (needs the docker socket for the docker backend)
#   worker:     docker run ... plex-loudness-worker          (needs the media, read-only)
FROM python:3.13-alpine

RUN apk add --no-cache ffmpeg docker-cli tini tzdata

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[tui]" && rm -rf /src

WORKDIR /work
ENV PLEX_LOUDNESS_CONFIG=/config/config.toml \
    TERM=xterm-256color
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["plex-loudness-worker"]
