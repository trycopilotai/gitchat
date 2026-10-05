#!/usr/bin/env python3
"""Render assets/social-preview.png from its HTML source.

GitHub renders the social card at 1280x640. This script is
the only thing that writes that PNG, so the committed asset
always has a source you can re-derive it from.

    python3 assets/build.py            # write the PNG
    python3 assets/build.py --check    # verify without writing

--check needs no browser: it compares the committed HTML
and PNG with the hashes in the stamp file and reads the
PNG's dimensions from its header, so it works on a machine
that cannot rebuild. The unittest suite makes the same
checks inline, so CI does not invoke this script.
"""

import argparse
import shutil
import struct
import subprocess
import sys
import time
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "social-preview.html"
TARGET = HERE / "social-preview.png"
# Records the SHA-256 of the HTML that produced the committed
# PNG *and* of the PNG itself, in `shasum -a 256` format.
# Without the first, the source can change while the committed
# render stays as it was, and the two drift apart silently.
# Without the second, the PNG is bound to nothing: any other
# 1280x640 image passes every check in the suite.
STAMP = HERE / "social-preview.sha256"
WIDTH, HEIGHT = 1280, 640

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


def png_size(path):
    """Read width and height out of the IHDR chunk."""
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} is not a PNG")
    return struct.unpack(">II", header[16:24])


def digest(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp_text():
    return "".join(
        f"{digest(path)}  {path.name}\n" for path in (SOURCE, TARGET)
    )


def read_stamp():
    """Parse the stamp into {filename: digest}."""
    if not STAMP.exists():
        raise SystemExit(f"missing {STAMP.name}; re-render to create it")
    recorded = {}
    for line in STAMP.read_text().splitlines():
        if line.strip():
            value, name = line.split()
            recorded[name] = value
    return recorded


def check_stamp():
    """Fail if either file changed after the PNG was rendered."""
    recorded = read_stamp()
    for path in (SOURCE, TARGET):
        if recorded.get(path.name) != digest(path):
            raise SystemExit(
                f"{path.name} does not match {STAMP.name}. Re-render."
            )


def find_chrome():
    for candidate in CHROME_CANDIDATES:
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def render(chrome):
    """Screenshot the source with headless Chrome.

    Chrome writes the PNG and then does not always exit, so
    this waits on the file rather than on the process and
    reaps it either way. Without the timeout the script hangs
    after having already done its job.
    """
    with tempfile.TemporaryDirectory() as tmp:
        before = TARGET.stat().st_mtime if TARGET.exists() else 0
        proc = subprocess.Popen(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--window-size={WIDTH},{HEIGHT}",
                f"--screenshot={TARGET}",
                f"--user-data-dir={tmp}",
                SOURCE.as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if TARGET.exists() and TARGET.stat().st_mtime > before:
                    # The PNG is written; Chrome may still linger.
                    time.sleep(1)
                    break
                time.sleep(0.5)
            else:
                raise SystemExit("Chrome did not produce a screenshot in 120s")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if not TARGET.exists() or TARGET.stat().st_mtime <= before:
            raise SystemExit(
                "Chrome exited without writing a new screenshot; "
                "the existing PNG was not re-stamped"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        if not TARGET.exists():
            raise SystemExit(f"missing {TARGET}")
        check_stamp()
        size = png_size(TARGET)
        if size != (WIDTH, HEIGHT):
            raise SystemExit(
                f"{TARGET.name} is {size[0]}x{size[1]}, "
                f"expected {WIDTH}x{HEIGHT}"
            )
        print(f"social-preview.png OK ({WIDTH}x{HEIGHT})")
        return

    chrome = find_chrome()
    if chrome is None:
        raise SystemExit(
            "no Chrome or Chromium found; install one to rebuild "
            "the social preview"
        )
    render(chrome)
    STAMP.write_text(stamp_text())
    size = png_size(TARGET)
    if size != (WIDTH, HEIGHT):
        raise SystemExit(
            f"rendered {size[0]}x{size[1]}, expected {WIDTH}x{HEIGHT}"
        )
    print(f"wrote {TARGET.name} ({WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    sys.exit(main())
