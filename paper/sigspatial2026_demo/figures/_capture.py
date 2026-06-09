"""Capture screenshots of the MeetHalfway demo for the SIGSPATIAL paper.

Outputs:
  - figures/cover.png      : Streamlit home/landing page
  - figures/result_map.png : pre-rendered demo map (demo_riverside_map.html)
"""
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]  # .../MeetHalfwayAI
FIG_DIR = Path(__file__).resolve().parent   # .../figures
DEMO_HTML = (ROOT / "demo_riverside_map.html").as_uri()

URL_HOME = "http://localhost:8502"


def capture():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000},
                                  device_scale_factor=2)
        page = ctx.new_page()

        # 1) Streamlit home page
        print("Loading", URL_HOME)
        page.goto(URL_HOME, wait_until="networkidle", timeout=60_000)
        # Streamlit re-renders; give it a beat for fonts/styles to settle
        page.wait_for_timeout(2500)
        out1 = FIG_DIR / "cover.png"
        page.screenshot(path=str(out1), full_page=True)
        print("  saved:", out1)

        # 2) Pre-rendered demo result map
        print("Loading", DEMO_HTML)
        page.goto(DEMO_HTML, wait_until="networkidle", timeout=60_000)
        # Folium tiles need a moment
        page.wait_for_timeout(3000)
        out2 = FIG_DIR / "result_map.png"
        page.screenshot(path=str(out2), full_page=False)
        print("  saved:", out2)

        browser.close()


if __name__ == "__main__":
    try:
        capture()
    except Exception as exc:
        print("ERR:", exc, file=sys.stderr)
        sys.exit(1)
