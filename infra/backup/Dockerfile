# Backup job for the office database: pg_dump -> age (public key only) -> local folder -> rclone.
# The private key that can open these files is NOT in this image or on this computer.
FROM postgres:16-alpine

RUN apk add --no-cache age rclone tzdata bash coreutils

ENV TZ=Europe/Istanbul

COPY infra/backup/backup.sh /usr/local/bin/klinik-backup
COPY infra/backup/schedule.sh /usr/local/bin/klinik-backup-schedule
RUN chmod 0755 /usr/local/bin/klinik-backup /usr/local/bin/klinik-backup-schedule

USER postgres
CMD ["klinik-backup-schedule"]
