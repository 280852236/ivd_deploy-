#!/bin/sh
echo "$(date '+%Y-%m-%d %H:%M:%S') Docker cleanup start"
docker image prune -a --filter "until=168h" --force 2>&1
docker builder prune --force 2>&1
docker volume prune --force 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') Docker cleanup done"
