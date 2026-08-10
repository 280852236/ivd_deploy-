#!/bin/sh
set -e

echo "\"${DB_USER:-ivd_user}\" \"${DB_PASSWORD:-ivd_pass}\"" > /etc/pgbouncer/userlist.txt

exec pgbouncer /etc/pgbouncer/pgbouncer.ini