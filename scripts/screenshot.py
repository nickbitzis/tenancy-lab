"""Capture README screenshots with headless Chrome. Start the server first: make run.

Usage: uv run python scripts/screenshot.py --port 8875
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]
DOCS = Path(__file__).resolve().parents[1] / "docs"


def chrome() -> str:
    for c in CHROME_PATHS:
        if Path(c).exists() or shutil.which(c):
            return c
    raise SystemExit("Chrome not found")


def shoot(url: str, out: Path, width: int, height: int, extra: tuple = ()) -> None:
    args = [
        chrome(),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        "--virtual-time-budget=8000",
        f"--screenshot={out}",
        *extra,
    ]
    subprocess.run([*args, url], check=True, capture_output=True, timeout=90)
    print(f"wrote {out}")


def shoot_phone(url: str, out: Path, width: int = 390, height: int = 1400) -> None:
    """Headless Chrome will not go below a 500px wide viewport, so render the page in a phone-sized
    iframe and crop the screenshot to it with macOS sips."""
    pad = (500 - width) // 2
    html = (
        f'<body style="margin:0 0 0 {pad}px;overflow:hidden">'
        f'<iframe src="{url}" style="width:{width}px;height:{height}px;border:0;display:block"></iframe></body>'
    )
    with tempfile.TemporaryDirectory() as profile:
        extra = ("--disable-web-security", "--disable-site-isolation-trials", f"--user-data-dir={profile}")
        shoot("data:text/html," + urllib.parse.quote(html), out, 500, height, extra)
    subprocess.run(["sips", "-c", str(height), str(width), str(out)], check=True, capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8875)
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}/"
    DOCS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        shoot(
            base,
            DOCS / "screenshot.png",
            1400,
            2000,
            ("--blink-settings=preferredColorScheme=1", f"--user-data-dir={profile}"),
        )
    with tempfile.TemporaryDirectory() as profile:
        shoot(
            base,
            DOCS / "screenshot-dark.png",
            1400,
            2000,
            ("--force-dark-mode", "--blink-settings=preferredColorScheme=0", f"--user-data-dir={profile}"),
        )
    shoot_phone(base, DOCS / "mobile.png")


if __name__ == "__main__":
    main()
