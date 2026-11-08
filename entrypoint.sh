#!/bin/sh
# serve: run this container's role. Anything else runs as a command (e.g. `plex-loudness status`).
set -e
if [ "$1" != "serve" ]; then
    exec "$@"
fi
role="${ROLE:-worker}"
uid="${PUID:-1000}"; gid="${PGID:-1000}"
name="plex-loudness"   # container_name in docker-compose.yml
case "$role" in
    worker|both)
        [ "$role" = "both" ] && echo "plex-loudness: controller ready -> docker exec -it $name plex-loudness"
        exec su-exec "$uid:$gid" plex-loudness-worker
        ;;
    controller)
        echo "plex-loudness $(plex-loudness --version): controller ready"
        echo "  open the TUI:   docker exec -it $name plex-loudness"
        echo "  or a command:   docker exec $name plex-loudness status"
        if ! docker version >/dev/null 2>&1; then
            echo "  WARNING: no docker socket - add docker-compose.controller.yml (COMPOSE_FILE in .env)"
        fi
        exec sleep infinity
        ;;
    *)
        echo "ROLE must be controller, worker or both (got '$role')" >&2
        exit 1
        ;;
esac
