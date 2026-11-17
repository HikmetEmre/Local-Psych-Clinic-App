-- Run on a NEW database server before restoring an office backup into it.
-- The backup references these roles (row-level security policies, grants);
-- they are server-wide and therefore not inside a database dump.
-- Passwords are set afterwards by: python -m klinik_local.manage set-role-passwords
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kasa_app') THEN
        CREATE ROLE kasa_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kasa_sync') THEN
        CREATE ROLE kasa_sync NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kasa_backup') THEN
        CREATE ROLE kasa_backup NOLOGIN BYPASSRLS;
    END IF;
END
$$;
GRANT pg_read_all_data TO kasa_backup;
