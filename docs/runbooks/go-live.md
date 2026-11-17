# Go-live checklist

## Blockers (decide before real client data goes in)

- [ ] **Cloud region chosen with a legal advisor (KVKK).** Supabase and Cloud Run in that region.
- [ ] Office laptop starts the stack after a reboot **without anyone logging in** (office-laptop-windows.md §2).
- [ ] BitLocker on; recovery key in the offline escrow.
- [ ] `LOCAL_FERNET_KEYS`, `LOCAL_AUTH_FERNET_KEY` and the age private key in the offline escrow (two copies).
- [ ] First nightly backup uploaded to Google Drive; first restore test **passed**.
- [ ] Clinical lead reviewed the default therapy catalog (TERAPILER_TAB §4.2) in Ayarlar → Terapi kataloğu.

## Definition of done (brief §16), and where each is proven

| Requirement | Automated proof | Manual check on the real system |
|---|---|---|
| B books for A from home; A's phone gets a push within seconds; A's open calendar updates without reload | `tests/e2e/test_live_update.py` (two real browsers), `test_cloud_notifications.py` (recipients, push delivery, retries) | B books a test appointment from home; A watches the phone and the open calendar |
| Two simultaneous bookings for the same slot: exactly one succeeds, the other gets a clear error | `test_cloud_appointments.py::test_concurrent_bookings_for_one_slot_exactly_one_wins` (20 at once) | — |
| Laptop offline: schedule and payments still work; the mirror catches up afterwards | `test_sync.py::test_office_down_the_mirror_catches_up` | Stop the laptop's internet for 10 min, book and pay in the cloud, reconnect, check Takvim in Kasa |
| Cloud offline: notes and logs still work; queued status updates sync automatically | `test_sync.py::test_cloud_down_work_continues_and_queue_drains_in_order` | — |
| B cannot read A's notes through any route; the cloud holds no names or free text | `test_local_terapiler_api.py` (every route + SQL-level RLS), `test_local_terapiler_pages.py`, `test_cloud_schema.py` | — |
| Latest backup restores | `test_backup_restore.py` (real script, brand-new server), `infra/backup/restore_test.sh` | Monthly, with the USB key |

## Day one

- [ ] Both therapists: first login in Kasa and in Web, new password, 2FA, recovery codes printed.
- [ ] Rooms entered in Klinik Web (Ayarlar → Odalar), default room per therapist.
- [ ] Android phones: Klinik Web added to the home screen, notifications on, test push received.
- [ ] Phones and home computers in the tailnet; Kasa opens at the `ts.net` address.
- [ ] Sync banner absent in Kasa; Ayarlar → Yedekleme shows no warnings.
