import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
artifacts = root / "artifacts"
artifacts.mkdir(exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    base_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:8000")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    workspace = page.request.get(f"{base_url}/api/workspace").json()
    people = [node for node in workspace["graph"]["nodes"] if node["kind"] == "person"]
    target = max(people, key=lambda node: len(node["event_ids"]))

    expect(page.locator(".topbar h1")).to_have_text("人物图")
    expect(page.locator('[data-testid="graph-canvas"] canvas').first).to_be_visible()
    page.locator('.search-box input[aria-label="搜索人物或关系"]').fill(target["label"])
    expect(page.locator(".inspector h2")).to_have_text(target["label"])
    page.locator(".segmented button").filter(has_text="全部节点").click()
    expect(page.locator(".graph-count")).to_contain_text("节点")
    page.screenshot(path=artifacts / "historybot-desktop.png", full_page=True)

    page.locator(".sidebar nav button").nth(3).click()
    expect(page.locator(".claim-detail h2")).to_be_visible()
    expect(page.locator(".evidence-block blockquote").first).to_be_visible()
    disputed_count = workspace["overview"]["status_counts"].get("disputed", 0)
    if disputed_count:
        page.locator(".claims-tools .segmented button").filter(has_text="存疑").click()
        page.locator(".claims-list > button").first.click()
        expect(page.locator(".claim-detail .status-disputed")).to_be_visible()
    else:
        expect(page.locator(".claims-list > button").first).to_be_visible()

    page.locator(".sidebar nav button").nth(1).click()
    expect(page.locator(".topbar h1")).to_have_text("时间线")
    expect(page.locator(".timeline-band")).to_be_visible()

    page.locator(".sidebar nav button").nth(2).click()
    expect(page.locator(".topbar h1")).to_have_text("地理线")
    expect(page.locator(".place-lines")).to_be_visible()

    page.locator(".sidebar nav button").nth(4).click()
    expect(page.locator(".topbar h1")).to_have_text("史料库")
    expect(page.locator(".source-table article")).to_have_count(len(workspace["sources"]))

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile.goto(base_url)
    mobile.wait_for_load_state("networkidle")
    expect(mobile.locator('[data-testid="graph-canvas"] canvas').first).to_be_visible()
    expect(mobile.locator(".sidebar nav")).to_be_visible()
    mobile.screenshot(path=artifacts / "historybot-mobile.png", full_page=True)

    assert not errors, errors
    browser.close()
