#!/bin/sh
set -eu
umask 077
[ "$(readlink -f /backups)" = /backups ] || exit 1
export PGPASSFILE=/tmp/pgpass
trap 'rm -f /tmp/pgpass' EXIT TERM INT
printf 'postgres:5432:quicklist:quicklist_admin:%s\n' "$(cat /run/secrets/postgres_admin_password)" > "$PGPASSFILE"
chmod 600 "$PGPASSFILE"
while :; do
  temporary=$(mktemp "/backups/quicklist-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX.tmp")
  name="${temporary%.tmp}.dump"
  pg_dump -h postgres -U quicklist_admin -d quicklist -Fc --no-owner --no-acl > "$temporary"
  pg_restore --list "$temporary" > /dev/null
  [ ! -e "$name" ] || exit 1
  mv "$temporary" "$name"
  sha256sum "$name" > "$name.sha256"
  date -u +%s > /backups/last-success
  # Local retention only; off-host encryption/copy is a release gate.
  find /backups -maxdepth 1 -type f -name 'quicklist-*.dump*' -mtime +14 -delete
  sleep 86400 & wait $!
done
