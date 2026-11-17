"""Build the Windows installer (main computer and connected computers; native, no Docker).

    uv run python scripts/build_installer.py            # stage + verify + compile
    uv run python scripts/build_installer.py --no-verify

Output: dist/NilPsikoloji-Klinik-Kurulum-<version>.exe

Contents: embeddable Python 3.12 with the office apps installed, PostgreSQL 16 binaries,
WinSW (Windows service wrapper), rclone (Google Drive), the setup scripts, and the clinic's
logo as icon and wizard images. Downloads are cached in build/cache.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
CACHE = BUILD / "cache"
STAGE = BUILD / "stage"
DIST = ROOT / "dist"
VERSION = "0.2.0"

PYTHON_VERSION = "3.12.8"
POSTGRES_VERSION = "16.12"
DOWNLOADS = {
    "python.zip": f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip",
    "postgresql.zip": f"https://get.enterprisedb.com/postgresql/postgresql-{POSTGRES_VERSION}-1-windows-x64-binaries.zip",
    "winsw.exe": "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe",
    "rclone.zip": "https://downloads.rclone.org/rclone-current-windows-amd64.zip",
}
# Parts of the PostgreSQL package the office does not need.
PG_SKIP = ("pgAdmin 4", "StackBuilder", "symbols", "doc", "include")
ISCC_CANDIDATES = (
    Path.home() / "AppData/Local/Programs/Inno Setup 6/ISCC.exe",
    Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
)


def log(text: str) -> None:
    print(f"==> {text}", flush=True)


# `uv run` tells us where uv is; otherwise it must be on PATH.
UV = os.environ.get("UV") or shutil.which("uv") or "uv"


def run(*command: str | Path, cwd: Path | None = None) -> None:
    subprocess.run([str(c) for c in command], check=True, cwd=cwd)


def download() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, url in DOWNLOADS.items():
        target = CACHE / name
        if target.exists() and target.stat().st_size > 0:
            continue
        log(f"downloading {url}")
        partial = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "klinik-build/1.0"})
        with urllib.request.urlopen(request, timeout=600) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.replace(target)
    for name in DOWNLOADS:
        digest = hashlib.sha256((CACHE / name).read_bytes()).hexdigest()
        print(f"    {name}: sha256 {digest}")


def stage_python() -> Path:
    log("staging Python")
    python_dir = STAGE / "python"
    with zipfile.ZipFile(CACHE / "python.zip") as z:
        z.extractall(python_dir)
    pth = next(python_dir.glob("python3*._pth"))
    pth.write_text(
        "\n".join([f"{pth.stem}.zip", ".", "Lib\\site-packages", "import site", ""]),
        encoding="ascii",
    )
    site = python_dir / "Lib" / "site-packages"
    site.mkdir(parents=True)

    wheels = BUILD / "wheels"
    shutil.rmtree(wheels, ignore_errors=True)
    for package in ("klinik-shared", "klinik-local"):
        run(UV, "build", "--package", package, "--wheel", "--out-dir", wheels, cwd=ROOT)
    requirements = BUILD / "requirements.txt"
    lines: set[str] = set()
    for package in ("klinik-local",):
        out = subprocess.run(
            [UV, "export", "--frozen", "--package", package, "--no-dev", "--no-emit-workspace",
             "--no-hashes", "--format", "requirements-txt"],
            check=True, capture_output=True, text=True, cwd=ROOT,
        ).stdout  # fmt: skip
        lines.update(line for line in out.splitlines() if line and not line.startswith(("#", " ")))
    requirements.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    target: list[str | Path] = [
        "--target",
        site,
        "--python-version",
        "3.12",
        "--python-platform",
        "x86_64-pc-windows-msvc",
    ]
    run(UV, "pip", "install", *target, "-r", requirements, cwd=ROOT)
    run(UV, "pip", "install", *target, "--no-deps", *sorted(wheels.glob("*.whl")), cwd=ROOT)
    for junk in site.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    return python_dir


def stage_postgres() -> None:
    log("staging PostgreSQL")
    with zipfile.ZipFile(CACHE / "postgresql.zip") as z:
        for member in z.namelist():
            parts = member.split("/")
            if len(parts) > 1 and parts[1] in PG_SKIP:
                continue
            z.extract(member, STAGE)
    assert (STAGE / "pgsql" / "bin" / "initdb.exe").exists()


def stage_tools() -> None:
    log("staging tools")
    tools = STAGE / "tools"
    tools.mkdir()
    shutil.copy(CACHE / "winsw.exe", tools / "winsw.exe")
    with zipfile.ZipFile(CACHE / "rclone.zip") as z:
        member = next(m for m in z.namelist() if m.endswith("/rclone.exe"))
        (tools / "rclone.exe").write_bytes(z.read(member))


def stage_app() -> None:
    log("staging migrations and setup scripts")
    app = STAGE / "app"
    shutil.copytree(ROOT / "migrations" / "local", app / "migrations" / "local",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    shutil.copy(ROOT / "installer" / "alembic.ini", app / "alembic.ini")
    setup = STAGE / "setup"
    setup.mkdir()
    for name in ("setup.ps1", "uninstall.ps1"):
        # Windows PowerShell 5.1 wants CRLF; the scripts are plain ASCII.
        text = (ROOT / "installer" / name).read_text(encoding="ascii")
        (setup / name).write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))


def stage_assets() -> None:
    log("making icon and wizard images from the logo")
    from PIL import Image

    assets = STAGE / "assets"
    assets.mkdir()
    logo = Image.open(ROOT / "assets" / "nil_psikoloji_logo.jpeg").convert("RGB")
    emblem = logo.crop((348, 8, 688, 348))
    emblem.save(
        assets / "klinik.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    beige = (251, 248, 242)
    tall = Image.new("RGB", (328, 628), beige)  # 2x of Inno's 164x314
    mark = emblem.resize((260, 260), Image.Resampling.LANCZOS)
    tall.paste(mark, (34, 120))
    wordmark = logo.crop((260, 390, 760, 470))
    wordmark = wordmark.resize(
        (300, int(300 * wordmark.height / wordmark.width)), Image.Resampling.LANCZOS
    )
    tall.paste(wordmark, (14, 410))
    tall.save(assets / "wizard.bmp")
    emblem.resize((110, 110), Image.Resampling.LANCZOS).save(assets / "wizard-small.bmp")


def verify() -> None:
    """Run what the installer ships, without services: database, migrations, app, jobs."""
    log("verifying the staged files (portable run, no services)")
    run(sys.executable, ROOT / "scripts" / "verify_stage.py", STAGE)


def compile_installer() -> Path:
    iscc = next((p for p in ISCC_CANDIDATES if p.exists()), None)
    if iscc is None:
        raise SystemExit("Inno Setup 6 (ISCC.exe) not found: winget install JRSoftware.InnoSetup")
    log("compiling the installer")
    DIST.mkdir(exist_ok=True)
    run(iscc, "/Q", f"/DStage={STAGE}", f"/DAppVersion={VERSION}", f"/DOutDir={DIST}",
        ROOT / "installer" / "klinik.iss")  # fmt: skip
    return DIST / f"NilPsikoloji-Klinik-Kurulum-{VERSION}.exe"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()
    download()
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    stage_python()
    stage_postgres()
    stage_tools()
    stage_app()
    stage_assets()
    if not args.no_verify:
        verify()
    exe = compile_installer()
    size = exe.stat().st_size / 1024 / 1024
    log(f"done: {exe} ({size:.0f} MB)")


if __name__ == "__main__":
    main()
