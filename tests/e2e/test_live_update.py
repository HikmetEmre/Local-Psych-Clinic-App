"""Definition of done (brief §16), in real browsers against the running dev stack:

  Therapist B books an appointment for A. A's open calendar updates without a reload,
  and A's bell shows the new notification.

Run with the dev stack up (see README):
    uv run pytest tests/e2e --e2e-users=path/to/state.json

The state file holds both dev users' passwords and TOTP secrets after first login:
    {"a": {"username": "terapist.a", "password": "...", "secret": "..."}, "b": {...}}
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyotp
import pytest
from playwright.sync_api import Page, expect, sync_playwright

WEB = "http://127.0.0.1:8001"
pytestmark = pytest.mark.e2e


@pytest.fixture
def users(request: pytest.FixtureRequest) -> dict[str, Any]:
    path = request.config.getoption("--e2e-users")
    if not path:
        pytest.skip("pass --e2e-users=<state.json> to run browser tests against the dev stack")
    return dict(json.loads(Path(path).read_text(encoding="utf-8")))


def login(page: Page, user: dict[str, str]) -> None:
    page.goto(f"{WEB}/login")
    page.fill("#username", user["username"])
    page.fill("#password", user["password"])
    page.click("main button[type=submit]")
    page.fill("#code", pyotp.TOTP(user["secret"]).now())
    page.click("main button[type=submit]")
    page.wait_for_url(f"{WEB}/takvim**")


def test_booking_appears_live_for_the_other_therapist(users: dict[str, Any]) -> None:
    day = date.today() + timedelta(days=14 - date.today().weekday())  # Monday in two weeks
    slot = "18:30"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        therapist_a = browser.new_context().new_page()
        therapist_b = browser.new_context().new_page()
        login(therapist_a, users["a"])
        login(therapist_b, users["b"])

        therapist_a.goto(f"{WEB}/takvim?gorunum=gun&gun={day.isoformat()}")
        bell = therapist_a.locator("a.bell")
        before = bell.inner_text()
        expect(therapist_a.locator("#calendar")).not_to_contain_text(f"{slot}–")

        therapist_b.goto(f"{WEB}/randevular/yeni")
        therapist_b.fill("#client_code", users["client_code"])
        therapist_b.select_option("#therapist_id", label=users["a"]["display_name"])
        therapist_b.fill("#date", day.isoformat())
        therapist_b.fill("#time", slot)
        therapist_b.select_option("#room_id", index=1)
        therapist_b.click("main form.card button[type=submit]")
        therapist_b.wait_for_url(f"{WEB}/takvim**")

        # A did nothing: the calendar and the bell change on their own.
        expect(therapist_a.locator("#calendar")).to_contain_text(f"{slot}–", timeout=10_000)
        expect(therapist_a.locator("#calendar")).to_contain_text(users["client_code"])
        expect(bell.locator(".bell__count")).to_be_visible(timeout=10_000)
        assert bell.inner_text() != before
        browser.close()
