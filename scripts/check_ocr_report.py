from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    output = Path("artifacts")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.goto(os.getenv("OCR_REPORT_URL", "http://127.0.0.1:4174/ocr_results.html"))
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".source")
        assert page.locator(".source").count() == 2
        assert page.get_by_text("Cao Cao crossed the river in 208 AD.").count() >= 1
        assert page.get_by_text("98%").count() >= 1
        assert page.get_by_text("需复核").count() >= 1
        page.screenshot(path=str(output / "ocr_results_desktop.png"), full_page=True)
        page.get_by_role("button", name="TrOCR 手写体").click()
        assert page.locator(".model-card").count() == 2
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".source")
        page.screenshot(path=str(output / "ocr_results_mobile.png"), full_page=True)
        assert page.locator(".source-body").first.bounding_box()["width"] > 0
        browser.close()


if __name__ == "__main__":
    main()
