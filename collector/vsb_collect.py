import re
from playwright.sync_api import Playwright, sync_playwright, expect
import json

def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    def log_response(r):
        print(r.status, r.url[:120])         
        if "class-data" in r.url:
            try:
                open("../data/raw/resp.xml", "w").write(r.text())
                print("  ^^ saved")
            except Exception as e:
                print(f"  ^^ couldn't read: {e}")

    page.on("response", log_response)
    page.goto("https://vsb.mcgill.ca/criteria.jsp?term=202609&course_0_0=COMP-250&nouser=1")
    page.wait_for_selector(".seatText", timeout=15000)
    rows = page.evaluate(r"""
  () => [...document.querySelectorAll('.seatText')].map(el => ({
    seats: parseInt(el.textContent),
    row: el.closest('tr, div').innerText.replace(/\s+/g,' ').trim()
    }))
    """)

    with open("../data/raw/data.json", "w") as f:
        json.dump(rows, f, indent=3)

    page.pause()

with sync_playwright() as playwright:
    run(playwright)