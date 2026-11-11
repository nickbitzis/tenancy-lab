"""Check that the web UI does not overflow sideways on a 390px wide phone.

Headless Chrome clamps --window-size to a 500px wide viewport, so the page is loaded in a 390px iframe and
measured from the parent. Elements inside .table-scroll may scroll inside their own box. Start the server
first: make run.

Usage: cd scripts && uv run python check_mobile.py --port 8875
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import urllib.parse

from screenshot import chrome

PAGE = """<!doctype html><body style="margin:0">
<iframe id="f" src="{url}" style="width:390px;height:900px;border:0"></iframe>
<pre id="out">pending</pre><script>
setTimeout(() => {{ try {{
  const d = document.getElementById("f").contentDocument;
  const w = d.documentElement.clientWidth;
  const wide = [...d.querySelectorAll("body *")]
    .filter((e) => e.getBoundingClientRect().right > w + 1 && !e.closest(".table-scroll") && !e.closest(".tooltip"))
    .slice(0, 5).map((e) => e.tagName + "." + (e.getAttribute("class") || ""));
  const sw = d.documentElement.scrollWidth;
  document.getElementById("out").textContent = "RESULT " + w + " " + sw + " " + JSON.stringify(wide);
}} catch (err) {{ document.getElementById("out").textContent = "ERR " + err; }} }}, 3000);
</script></body>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8875)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as profile:
        html = PAGE.format(url=f"http://127.0.0.1:{a.port}/")
        args = [
            chrome(),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--window-size=390,900",
            "--virtual-time-budget=6000",
            "--disable-web-security",
            "--disable-site-isolation-trials",
            f"--user-data-dir={profile}",
            "--dump-dom",
            "data:text/html," + urllib.parse.quote(html),
        ]
        out = subprocess.run(args, capture_output=True, text=True, timeout=90).stdout
    m = re.search(r"RESULT (\d+) (\d+) (.*?)</pre>", out)
    if not m:
        err = re.search(r'<pre id="out">(.*?)</pre>', out)
        sys.exit(f"could not measure: {err[1] if err else 'no output'}")
    width, scroll, wide = int(m[1]), int(m[2]), m[3].replace("&quot;", '"')
    ok = scroll <= width and wide == "[]"
    print(f"viewport {width}px, content {scroll}px  {'ok' if ok else 'OVERFLOW ' + wide}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
