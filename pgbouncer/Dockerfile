FROM alpine:3.18

RUN apk add --no-cache pgbouncer postgresql-client

RUN adduser -D -s /bin/sh pgbouncer && mkdir -p /etc/pgbouncer && chown -R pgbouncer:pgbouncer /etc/pgbouncer

COPY entrypoint.sh /entrypoint.sh
COPY config/pgbouncer.ini /etc/pgbouncer/pgbouncer.ini

RUN chmod +x /entrypoint.sh && chown pgbouncer:pgbouncer /entrypoint.sh /etc/pgbouncer/pgbouncer.ini

USER pgbouncer

ENTRYPOINT ["/entrypoint.sh"]
