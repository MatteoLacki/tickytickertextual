"""Run against browser_fixture.py using an isolated Playwright environment."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width":1800,"height":1000},
                                            permissions=["clipboard-read","clipboard-write"], accept_downloads=True)
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("http://127.0.0.1:18979")
        await page.wait_for_function("!document.querySelector('#export-toolbar button').disabled")
        await page.get_by_role("button", name="Copy table").click()
        await page.wait_for_function("document.querySelector('#export-status').textContent === 'copied'")
        copied = await page.evaluate("navigator.clipboard.readText()")
        assert "1234567890123456789" in copied
        assert "QQ / QQ-HeLa" in copied and "HeLa browser reference" in copied
        # Exercise the same fallback used on plain LAN HTTP.
        await page.evaluate("Object.defineProperty(window, 'isSecureContext', {value:false, configurable:true})")
        await page.get_by_role("button", name="Copy table").click()
        await page.wait_for_function("document.querySelector('#export-status').textContent === 'copied'")
        assert "1234567890123456789" in await page.evaluate("navigator.clipboard.readText()")
        async with page.expect_download() as download_info:
            await page.get_by_role("button", name="Save table").click()
        download = await download_info.value
        assert download.suggested_filename == "selected.csv"
        assert "1234567890123456789" in Path(await download.path()).read_text()
        terminal = await page.locator("#terminal").bounding_box()
        toolbar = await page.locator("#export-toolbar").bounding_box()
        assert toolbar["y"] >= terminal["y"] + terminal["height"]
        await page.screenshot(path="/tmp/tickyticker-browser-main.png")
        assert await page.get_by_role("button", name="Rerun", exact=True).is_enabled()
        async with page.expect_popup() as popup_info:
            await page.get_by_role("button", name="See Chromatograms", exact=True).click()
        viewer = await popup_info.value
        await viewer.wait_for_selector("svg")
        assert "sample.d" in await viewer.locator("#plot").text_content()
        assert "Sample <&> description" in await viewer.locator("#plot").text_content()
        async with viewer.expect_download() as download_info:
            await viewer.get_by_role("button", name="Download hi-res SVG").click()
        download = await download_info.value
        assert download.suggested_filename == "chromatograms.svg"
        assert "sample.d" in Path(await download.path()).read_text()
        await viewer.close()
        # Rerun opens exactly one dialog and can be cancelled before reopening plots.
        await page.get_by_role("button", name="Rerun", exact=True).click()
        await page.wait_for_timeout(300)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        async with page.expect_popup() as repeat_info:
            await page.get_by_role("button", name="See Chromatograms", exact=True).click()
        repeated = await repeat_info.value
        await repeated.wait_for_selector("svg")
        await repeated.close()
        await page.locator(".xterm-helper-textarea").focus()
        async with page.expect_download() as download_info:
            await page.keyboard.press("F5")
        download = await download_info.value
        assert download.suggested_filename == "event-histogram.svg"
        assert "Raw-event intensity histogram" in Path(await download.path()).read_text()
        for _ in range(2):
            async with page.expect_download() as large_info:
                await page.keyboard.press("F6")
            large = await large_info.value
            assert large.suggested_filename == "dominant-charge.svg"
            import xml.etree.ElementTree as ET
            ET.parse(await large.path())
        await page.keyboard.press("F2")
        await page.wait_for_timeout(500)
        await page.keyboard.press("ArrowRight")
        await page.wait_for_timeout(500)
        await page.screenshot(path="/tmp/tickyticker-browser-review.png")
        # Restart remains clickable even while a modal review is open.
        await page.get_by_role("button", name="Restart app", exact=True).click()
        await page.wait_for_function("document.body.classList.contains('-first-byte')")
        await page.wait_for_function("!document.querySelector('[data-action=chromatograms]').disabled")
        assert not errors, errors
        await browser.close()
        print("PASS: clipboard (secure/fallback), full precision CSV, toolbar placement, combined viewer, repeated SVG downloads, Rerun cancellation, review navigation, modal restart")


if __name__ == "__main__":
    asyncio.run(main())
