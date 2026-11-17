"""Background jobs of the office tier: backups, the daily snapshot, the restore test.

On the native Windows installation these run in one service (KlinikJobs):

    python -m klinik_local.jobs run                 scheduler (the service)
    python -m klinik_local.jobs backup daily|hourly
    python -m klinik_local.jobs snapshot
    python -m klinik_local.jobs restore-test --identity <age key file> [--file <backup>]
"""
