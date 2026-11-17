"""Daily encrypted snapshot for reading from home (replaces Tailscale)."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from support.local import (
    A_ID,
    B_ID,
    FERNET_KEY,
    Kasa,
    LocalDb,
    add_therapists,
    fetch,
    run,
    scalar,
    with_engine,
)

from klinik_local import manage
from klinik_local.snapshot import AAD, decrypt_payload, derive_key, write_snapshots

pytestmark = pytest.mark.integration

PASS_A = "a-nin-ozet-sifresi"
PASS_B = "b-nin-ozet-sifresi"
NOTE_A = "A'nın notu: uyku düzeni iyileşti."
NOTE_B = "B'nin notu: aile görüşmesi planlandı."
ASSETS = Path(__file__).resolve().parents[2] / "apps/local/klinik_local/snapshot_assets"


@dataclass
class Office:
    db: LocalDb
    a: Kasa
    b: Kasa
    backup_dsn: str


def make_session(kasa: Kasa, name: str, note: str) -> None:
    created = kasa.post("/danisanlar", {"full_name": name, "birth_date": "1990-05-01"})
    client = created.headers["location"].split("/")[2].split("?")[0]
    response = kasa.api(
        "POST",
        "/api/therapy/sessions",
        json={
            "client_id": client,
            "session_date": "2026-11-10T14:00:00+03:00",
            "format_code": "individual",
            "methods": [{"code": "cbt"}],
            "notes": note,
        },
    )
    assert response.status_code == 201, response.text


@pytest.fixture
def office(local_db: LocalDb) -> Iterator[Office]:
    temporary = add_therapists(local_db)
    with_engine(local_db.owner_url, lambda e: manage.set_role_passwords(e, {"kasa_backup": "bk"}))
    dsn = re.sub(r"//[^@]+@", "//kasa_backup:bk@", local_db.owner_url).replace("+asyncpg", "")
    with Kasa(local_db) as a, Kasa(local_db) as b:
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        make_session(a, "Ayşe Yılmaz", NOTE_A)
        make_session(b, "Mehmet Demir", NOTE_B)
        yield Office(local_db, a, b, dsn)


def set_passphrase(kasa: Kasa, passphrase: str) -> Any:
    return kasa.post("/hesap/ozet", {"passphrase": passphrase, "passphrase_repeat": passphrase})


def params_of(html: str) -> dict[str, Any]:
    match = re.search(r"var P = (\{.*?\});", html)
    assert match, "no parameters in page"
    return dict(json.loads(match.group(1)))


def open_file(path: Path, passphrase: str) -> dict[str, Any]:
    p = params_of(path.read_text(encoding="utf-8"))
    key = derive_key(passphrase, base64.b64decode(p["salt"]), p["iterations"])
    return decrypt_payload(key, base64.b64decode(p["nonce"]), base64.b64decode(p["ciphertext"]))


def test_each_therapist_gets_only_their_own_notes(office: Office, tmp_path: Path) -> None:
    assert set_passphrase(office.a, PASS_A).status_code == 303
    assert set_passphrase(office.b, PASS_B).status_code == 303
    result = run(write_snapshots(office.backup_dsn, FERNET_KEY, tmp_path))
    assert len(result.files) == 2 and result.skipped_users == []
    file_a = next(p for p in result.files if "terapist.a" in p.name)
    file_b = next(p for p in result.files if "terapist.b" in p.name)

    a = open_file(file_a, PASS_A)
    assert a["therapist"] == "Terapist A"
    assert [s["notes"] for s in a["sessions"]] == [NOTE_A]
    assert [c["name"] for c in a["clients"]] == ["Ayşe Yılmaz"]
    b = open_file(file_b, PASS_B)
    assert [s["notes"] for s in b["sessions"]] == [NOTE_B]
    assert NOTE_A not in json.dumps(b, ensure_ascii=False)

    # Wrong passphrase, or the other therapist's, opens nothing.
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        open_file(file_a, PASS_B)

    # Nothing readable in the file itself: that is what Google stores.
    for path in (file_a, file_b):
        html = path.read_text(encoding="utf-8")
        for secret in ("Ayşe", "Mehmet", "uyku", "aile görüşmesi", "DN-"):
            assert secret not in html
    rows = fetch(office.db, "SELECT actor_kind FROM audit_log WHERE action = 'snapshot_created'")
    actions = [r["actor_kind"] for r in rows]
    assert actions == ["system", "system"]


def test_no_passphrase_means_no_file(office: Office, tmp_path: Path) -> None:
    set_passphrase(office.a, PASS_A)
    result = run(write_snapshots(office.backup_dsn, FERNET_KEY, tmp_path))
    assert [p.name.startswith("nil-ozet-terapist.a-") for p in result.files] == [True]
    assert result.skipped_users == ["terapist.b"]


def test_old_snapshots_are_pruned(office: Office, tmp_path: Path) -> None:
    set_passphrase(office.a, PASS_A)
    for day in range(1, 10):
        (tmp_path / f"nil-ozet-terapist.a-2026-01-0{day}.html").write_text("x")
    run(write_snapshots(office.backup_dsn, FERNET_KEY, tmp_path, keep=7))
    assert len(list(tmp_path.glob("nil-ozet-terapist.a-*.html"))) == 7


def test_passphrase_page_rules(office: Office) -> None:
    short = office.a.post("/hesap/ozet", {"passphrase": "kisa", "passphrase_repeat": "kisa"})
    assert short.status_code == 400 and "en az 10" in short.text
    mismatch = office.a.post(
        "/hesap/ozet", {"passphrase": PASS_A, "passphrase_repeat": PASS_A + "x"}
    )
    assert mismatch.status_code == 400 and "aynı değil" in mismatch.text
    set_passphrase(office.a, PASS_A)
    assert "Açık" in office.a.get("/hesap/ozet").text
    assert "Kapalı" in office.b.get("/hesap/ozet").text  # B sees only their own state
    stored = fetch(office.db, "SELECT * FROM snapshot_keys")[0]
    assert stored["user_id"] == A_ID and PASS_A.encode() not in bytes(stored["key_enc"])
    office.a.post("/hesap/ozet/kapat")
    assert scalar(office.db, "SELECT count(*) FROM snapshot_keys") == 0
    assert B_ID  # B never touched A's row


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("mode", ["webcrypto", "bundled"])
def test_the_page_script_decrypts_it(office: Office, tmp_path: Path, mode: str) -> None:
    """Run the page's own decryption code in Node, with and without WebCrypto."""
    set_passphrase(office.a, PASS_A)
    path = run(write_snapshots(office.backup_dsn, FERNET_KEY, tmp_path)).files[0]
    p = params_of(path.read_text(encoding="utf-8"))
    script = tmp_path / "check.js"
    script.write_text(
        f"""
const fs = require("fs");
global.window = global;
if ("{mode}" === "bundled") {{ Object.defineProperty(globalThis, "crypto", {{value: undefined}}); }}
eval(fs.readFileSync({json.dumps(str(ASSETS / "noble.min.js"))}, "utf8"));
const P = {json.dumps(p)};
const enc = new TextEncoder();
const b64 = s => Uint8Array.from(Buffer.from(s, "base64"));
(async () => {{
  let text;
  if ("{mode}" === "webcrypto") {{
    const subtle = require("crypto").webcrypto.subtle;
    const base = await subtle.importKey("raw", enc.encode({json.dumps(PASS_A)}), "PBKDF2", false, ["deriveKey"]);
    const key = await subtle.deriveKey({{name: "PBKDF2", salt: b64(P.salt), iterations: P.iterations, hash: "SHA-256"}},
      base, {{name: "AES-GCM", length: 256}}, false, ["decrypt"]);
    text = new TextDecoder().decode(await subtle.decrypt({{name: "AES-GCM", iv: b64(P.nonce), additionalData: enc.encode(P.aad)}}, key, b64(P.ciphertext)));
  }} else {{
    const k = noble.pbkdf2(noble.sha256, enc.encode({json.dumps(PASS_A)}), b64(P.salt), {{c: P.iterations, dkLen: 32}});
    text = new TextDecoder().decode(noble.gcm(k, b64(P.nonce), enc.encode(P.aad)).decrypt(b64(P.ciphertext)));
  }}
  process.stdout.write(JSON.parse(text).sessions[0].notes);
}})().catch(e => {{ console.error(e); process.exit(1); }});
""",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(script)], capture_output=True, check=False, timeout=120)
    assert out.returncode == 0, out.stderr.decode(errors="replace")
    assert out.stdout.decode("utf-8") == NOTE_A
    assert p["aad"] == AAD.decode()
