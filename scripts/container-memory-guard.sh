#!/bin/sh
THRESHOLD="${MEM_GUARD_THRESHOLD:-85}"
LOG="/backups/memory-guard.log"
echo "$(date '+%Y-%m-%d %H:%M:%S') memory guard check (threshold=${THRESHOLD}%)" >> "$LOG"
for name in web worker beat pgbouncer; do
  container="ivd_deploy-${name}-1"
  stats=$(docker stats --no-stream --format "{{.MemPerc}}" "$container" 2>/dev/null | tr -d '%')
  if [ -n "$stats" ]; then
    int=$(echo "$stats" | awk '{printf "%d", $1}')
    if [ "$int" -gt "$THRESHOLD" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') WARNING: $container at ${stats}%, restarting" >> "$LOG"
      docker restart "$container" >> "$LOG" 2>&1
    fi
  fi
done
