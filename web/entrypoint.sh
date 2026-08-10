#!/bin/sh
if [ "$(id -u)" = "0" ]; then
    chown -R 1001:1001 /app/uploads 2>/dev/null
    chown 1001:1001 /app/ivd_app.log 2>/dev/null
    exec su appuser -s /bin/sh -c "exec $*"
else
    exec "$@"
fi