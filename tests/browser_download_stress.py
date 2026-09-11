"""Exercise a slow, large hi-res download while interacting with the app."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1800, "height": 1000}, accept_downloads=True)
        page = await context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto('http://127.0.0.1:18979')
        await page.get_by_role('button', name='oh my tickyticker', exact=True).click()
        await page.wait_for_function("exportState.actions.help === true")
        await page.locator('.xterm-helper-textarea').focus()
        await page.keyboard.press('F2')
        await page.wait_for_timeout(300)
        async with page.expect_download(timeout=15000) as pending:
            # Click the actual Textual download button in this fixed-size review.
            await page.mouse.click(1100, 845)
            await page.wait_for_function('exportState.actions.chromatograms === false', timeout=2000)
            await page.get_by_role('button', name='Help', exact=True).click()
            # Help must respond before the intentionally slow renderer completes.
            await page.wait_for_function("exportState.actions.help === false", timeout=2000)
            await page.screenshot(path='/tmp/tickyticker-slow-download-help.png')
            await page.keyboard.press('Escape')
        download = await pending.value
        path = Path(await download.path())
        content = path.read_text()
        assert content.count('<rect ') >= 150000
        assert download.suggested_filename == 'dominant-charge.svg'
        await page.wait_for_function("exportState.actions.help === true")
        await page.get_by_role('button', name='Help', exact=True).click()
        await page.wait_for_function("exportState.actions.help === false")
        assert not errors, errors
        print(f'PASS: responsive before/during/after {path.stat().st_size:,}-byte SVG download')
        await browser.close()

asyncio.run(main())
