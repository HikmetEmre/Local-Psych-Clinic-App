"""Compose smoke test: every service answers /healthz with a connected database.

python scripts/smoke_healthz.py
"""

from __future__ import annotations

import json
import sys
import urllib.request

SERVICES = {
    "klinik-cloud": "http://127.0.0.1:8001/healthz",
    "klinik-local": "http://127.0.0.1:8002/healthz",
    "klinik-sync": "http://127.0.0.1:8003/healthz",
}


def main() -> int:
    failed = False
    for service, url in SERVICES.items():
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                body = json.load(response)
        except Exception as exc:
            print(f"FAIL {service}: {exc}")
            failed = True
            continue
        ok = body == {"status": "ok", "service": service, "db": "ok"}
        print(f"{'ok  ' if ok else 'FAIL'} {service}: {body}")
        failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
