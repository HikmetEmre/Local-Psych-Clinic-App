#!/usr/bin/env bash
# Runs inside the backup container (TZ=Europe/Istanbul):
#   02:30 every night          -> daily backup (kept 30 days, plus 12 monthly)
#   every hour 09:00 - 21:00   -> hourly backup during working hours (kept 48)
#   Sunday 03:30               -> encrypted copy of the cloud database (kept 8), if configured
# A plain loop instead of cron: no daemon to configure, and logs go to `docker logs`.
set -uo pipefail

last_daily=""
last_hourly=""
last_cloud=""
echo "backup scheduler started ($(date))"
while true; do
  today="$(date +%Y-%m-%d)"
  hour="$(date +%H)"
  minute="$(date +%M)"
  if [ "$hour$minute" \> "0229" ] && [ "$last_daily" != "$today" ]; then
    klinik-backup daily && last_daily="$today"
  fi
  if [ "$hour" \> "08" ] && [ "$hour" \< "22" ] && [ "$last_hourly" != "$today-$hour" ]; then
    klinik-backup hourly && last_hourly="$today-$hour"
  fi
  if [ -n "${CLOUD_DUMP_URL:-}" ] && [ "$(date +%u)" = "7" ] && [ "$hour$minute" \> "0329" ] \
     && [ "$last_cloud" != "$today" ]; then
    klinik-backup cloud && last_cloud="$today"
  fi
  sleep 60
done
