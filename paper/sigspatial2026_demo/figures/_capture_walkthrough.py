"""Capture Streamlit Check Result page with playwright for §4 Panel A."""
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT_DIR = Path(r"D:\Meet halfway\MeetHalfwayAI\paper\sigspatial2026_demo\figures")
RAW_PNG = OUT_DIR / "ui_walkthrough_raw.png"
FINAL_PNG = OUT_DIR / "ui_walkthrough.png"
URL = "http://localhost:8502/?demo_seed=riverside_n3"


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            channel=None,
        )
        ctx = browser.new_context(
            viewport={"width": 900, "height": 2200},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        # Wait for streamlit to be ready (websocket finishes & content renders)
        page.wait_for_function(
            "() => !!document.querySelector('h3') && document.body.innerText.includes('Recommended Venues')",
            timeout=30000,
        )
        # Wait for folium iframe to load
        page.wait_for_selector("iframe[title*='folium']", timeout=20000)
        # Give markers/polygons a real wall-clock window to render
        time.sleep(6)
        # Ask the folium map to invalidate size + fit bounds in case it laid out before reflow
        try:
            page.evaluate(
                """
                () => {
                  const iframe = document.querySelector('iframe[title*=\"folium\"]');
                  if (!iframe) return;
                  const win = iframe.contentWindow;
                  const m = win.map_div;
                  if (!m) return;
                  m.invalidateSize();
                }
                """
            )
        except Exception as e:
            print("invalidateSize warn:", e)
        time.sleep(2)
        page.screenshot(path=str(RAW_PNG), full_page=True)
        print("raw saved", RAW_PNG, RAW_PNG.stat().st_size, "bytes")
        browser.close()


if __name__ == "__main__":
    main()
