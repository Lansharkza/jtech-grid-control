#!/bin/sh
# Runs as root only long enough to make the mounted data folder writable, then
# drops to an unprivileged user for the application itself.
#
# On Synology a bind-mounted folder arrives owned by root, and the app cannot
# write to it. Rather than run the whole app as root, fix the ownership here.

set -e

APP_UID=1000
APP_GID=1000

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/data/certs
    # Only chown when it is actually wrong — on a large history this is slow.
    if [ "$(stat -c %u /app/data)" != "$APP_UID" ]; then
        echo "entrypoint: taking ownership of /app/data for uid $APP_UID"
        chown -R "$APP_UID:$APP_GID" /app/data
    fi
    exec gosu "$APP_UID:$APP_GID" "$@"
fi

# Already unprivileged (someone set `user:` in compose) — just run.
exec "$@"
