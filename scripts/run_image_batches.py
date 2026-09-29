"""Run image-ingest batches back to back, unattended.

`ingest_property_images.py` is checkpointed and resumable, so a long backfill
is just that script invoked repeatedly. This drives it: it waits for any run
already in flight, then executes batches in sequence, logging each one and
carrying on if a single batch fails.

Batches rather than one enormous run because each invocation re-warms the
dedupe table from MongoDB and commits its checkpoint, so a crash costs one
batch instead of the whole night. Between batches it also syncs
`properties.images[].blob_url`, which is what the API actually reads — that
way a run interrupted halfway still leaves every finished property serving
its own images rather than waiting for a step at the end that never came.

Usage:
    python scripts/run_image_batches.py --batches 3 --size 2000
    python scripts/run_image_batches.py --batches 5 --size 2000 --wait-first
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Unbuffered: this log is read while the run is still going, and a
# buffered one stays empty for minutes at a time.
sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():                      # non-Windows checkout
    PY = Path(sys.executable)
INGEST = ROOT / "scripts" / "ingest_property_images.py"
SYNC = ROOT / "scripts" / "sync_blob_urls.py"
LOG_DIR = ROOT / "data" / "logs"


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _progress() -> int:
    """How many properties are ingested right now."""
    import asyncio

    sys.path.insert(0, str(ROOT / "src"))
    from homz.db.mongo import close_client, get_database

    async def go() -> int:
        db = get_database()
        try:
            return await db["property_images"].count_documents({})
        finally:
            await close_client()

    return asyncio.run(go())


def _ingest_running(window: int = 45) -> bool:
    """Is another ingest in flight?

    Detected by watching progress rather than the process table: `wmic` is
    gone from current Windows and `tasklist`'s arguments get mangled when the
    shell is Git Bash, so neither is dependable here. Ingested-property count
    rising over a short window is a direct signal that work is happening, and
    it works the same on any OS.

    Two concurrent runs would not corrupt anything — writes are upserts and
    uploads are content-addressed — but they would walk the same checkpoint
    and duplicate most of the work.
    """
    try:
        before = _progress()
        time.sleep(window)
        return _progress() > before
    except Exception:  # noqa: BLE001 - never block the batch run on this check
        return False


def _run(cmd: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT)
    return proc.returncode


def main(batches: int, size: int, wait_first: bool, sync: bool) -> None:
    started = time.monotonic()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if wait_first:
        waited = 0
        while _ingest_running():
            if waited % 300 == 0:
                print(f"[{_stamp()}] waiting for the running batch to finish "
                      f"({waited // 60} min so far)")
            time.sleep(30)
            waited += 30
        if waited:
            print(f"[{_stamp()}] previous batch finished after {waited // 60} min")

    for n in range(1, batches + 1):
        log = LOG_DIR / f"batch_{datetime.now():%Y%m%d-%H%M%S}.log"
        print(f"[{_stamp()}] batch {n}/{batches}: {size} properties -> {log.name}")
        t0 = time.monotonic()
        code = _run([str(PY), str(INGEST), "--limit", str(size)], log)
        mins = (time.monotonic() - t0) / 60

        tail = ""
        try:
            lines = [ln for ln in log.read_text(encoding="utf-8",
                                                errors="replace").splitlines()
                     if ln.strip() and not ln.startswith("20")]
            tail = next((ln for ln in reversed(lines) if ln.startswith("done:")), "")
        except OSError:
            pass
        print(f"[{_stamp()}] batch {n} finished in {mins:.0f} min (exit {code})")
        if tail:
            print(f"           {tail.strip()}")
        if code != 0:
            # Keep going: one bad batch is usually a transient network fault,
            # and the checkpoint means the next run picks up where it stopped.
            print(f"[{_stamp()}] non-zero exit — continuing to the next batch")

        if sync:
            slog = LOG_DIR / f"sync_{datetime.now():%Y%m%d-%H%M%S}.log"
            _run([str(PY), str(SYNC)], slog)
            try:
                last = [ln for ln in slog.read_text(encoding="utf-8",
                                                    errors="replace").splitlines()
                        if "serving Blob URLs" in ln]
                if last:
                    print(f"           {last[-1].strip()}")
            except OSError:
                pass

    print(f"[{_stamp()}] all {batches} batches done in "
          f"{(time.monotonic() - started) / 3600:.1f} h")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=3)
    ap.add_argument("--size", type=int, default=2000)
    ap.add_argument("--wait-first", action="store_true",
                    help="wait for an ingest already running before starting")
    ap.add_argument("--no-sync", dest="sync", action="store_false",
                    help="skip the blob_url sync between batches")
    a = ap.parse_args()
    main(a.batches, a.size, a.wait_first, a.sync)
