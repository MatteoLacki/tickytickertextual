"""Run against browser_fixture.py using an isolated Playwright environment."""
import asyncio
import time
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
        welcome = page.get_by_role('dialog', name='Welcome to tickyticker')
        await welcome.wait_for(state='visible')
        expected_logo = (Path(__file__).parents[1] / 'logoacsii.txt').read_text()
        assert await page.locator('#welcome-logo').text_content() == expected_logo
        assert not await page.locator('#browser-toolbar').is_visible()
        await page.keyboard.press('Escape')
        assert await welcome.is_visible()
        await page.screenshot(path='/tmp/tickyticker-browser-welcome.png')
        await page.get_by_role('button', name='oh my tickyticker', exact=True).click()
        assert not await welcome.is_visible()
        await page.wait_for_function("!document.querySelector('#browser-toolbar button[data-export]').disabled")
        boxes = await page.locator('#browser-toolbar > button').evaluate_all('(buttons) => buttons.map(b => ({x:b.offsetLeft, y:b.offsetTop, w:b.offsetWidth, h:b.offsetHeight}))')
        assert len(boxes) == 9
        assert max(b['w'] for b in boxes) - min(b['w'] for b in boxes) <= 1
        assert len({b['y'] for b in boxes}) == len({b['h'] for b in boxes}) == 1
        await page.locator('.xterm-helper-textarea').focus()
        await page.keyboard.press('Control+ArrowDown')
        await page.wait_for_function('exportState.actions.estimate === true')
        selection_latencies = []
        for key, expected in [('ArrowDown', 'false'), ('ArrowUp', 'true')]:
            start = time.monotonic()
            await page.keyboard.press(key)
            await page.wait_for_function(f'exportState.actions.estimate === {expected}', timeout=750)
            selection_latencies.append(time.monotonic() - start)
        print('Selection button updates (ms):', [round(t * 1000) for t in selection_latencies])
        await page.get_by_role("button", name="Copy table").click()
        assert await page.get_by_role("button", name="Copy table").is_disabled()
        await page.evaluate("updateExportButtons()")
        assert await page.get_by_role("button", name="Copy table").is_disabled()
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
        assert await page.get_by_role("button", name="Save table").is_disabled()
        assert download.suggested_filename == "selected.csv"
        assert "1234567890123456789" in Path(await download.path()).read_text()
        terminal = await page.locator("#terminal").bounding_box()
        toolbar = await page.locator("#browser-toolbar").bounding_box()
        assert toolbar["y"] >= terminal["y"] + terminal["height"]
        await page.screenshot(path="/tmp/tickyticker-browser-main.png")
        assert await page.get_by_role("button", name="Rerun", exact=True).is_enabled()
        await page.get_by_role("button", name="Help", exact=True).click()
        await page.wait_for_function("exportState.actions.help === false")
        await page.keyboard.press("Escape")
        await page.wait_for_function("exportState.actions.estimate === true")
        await page.get_by_role("button", name="Estimate charge split", exact=True).click()
        await page.wait_for_function("exportState.actions.estimate === false")
        await page.keyboard.press("Escape")
        await page.wait_for_function("exportState.actions.estimate === true")
        async with page.expect_popup() as popup_info:
            await page.get_by_role("button", name="See Chromatograms", exact=True).click()
        viewer = await popup_info.value
        await viewer.wait_for_selector("svg")
        assert "sample.d" in await viewer.locator("#plot").text_content()
        assert "multicharge" in await viewer.locator("#plot").text_content()
        assert "singly charge" in await viewer.locator("#plot").text_content()
        assert "Sample <&> description" in await viewer.locator("#plot").text_content()
        async with viewer.expect_download() as download_info:
            await viewer.get_by_role("button", name="Download hi-res SVG").click()
        download = await download_info.value
        assert await viewer.get_by_role("button", name="Download hi-res SVG").is_disabled()
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
        await welcome.wait_for(state='visible')
        assert not await page.locator('#browser-toolbar').is_visible()
        await page.get_by_role('button', name='oh my tickyticker', exact=True).click()
        await page.wait_for_function("document.body.classList.contains('-first-byte')")
        await page.wait_for_function("!document.querySelector('[data-action=chromatograms]').disabled")
        assert not errors, errors
        await browser.close()
        print("PASS: clipboard (secure/fallback), full precision CSV, toolbar placement, combined viewer, repeated SVG downloads, Rerun cancellation, review navigation, modal restart")


if __name__ == "__main__":
    asyncio.run(main())
