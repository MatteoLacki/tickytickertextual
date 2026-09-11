"""Exercise manual separator input and acceptance with slow TIC initialization."""
import asyncio
import csv
import time
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
        await page.wait_for_function('exportState.ready')
        await page.locator('.xterm-helper-textarea').focus()
        await page.keyboard.press('F3')
        await page.wait_for_timeout(200)
        await page.keyboard.type('1.6', delay=60)
        await page.keyboard.press('Tab')
        await page.wait_for_timeout(200)
        await page.keyboard.type('-0.001', delay=60)
        await page.wait_for_timeout(300)
        await page.screenshot(path='/tmp/tickyticker-manual-separator.png')
        await page.wait_for_function('exportState.actions["tic-new"] === true', timeout=3000)
        await page.get_by_role('button', name='TIC new folders', exact=True).click()
        await page.wait_for_function('exportState.actions["tic-new"] === false')
        await page.get_by_role('button', name='Help', exact=True).click()
        await page.wait_for_function('exportState.actions.help === false', timeout=1000)
        await page.keyboard.press('Escape')
        await page.wait_for_function('exportState.ready')
        async with page.expect_download() as pending:
            await page.get_by_role('button', name='Save table', exact=True).click()
        rows = list(csv.DictReader(Path(await (await pending.value).path()).open()))
        assert len(rows) == 2 and all(row['QQ'] == '1600' for row in rows)
        assert 'Fit Parameters' not in rows[0]
        # Accepting an estimate replaces the manually entered separator.
        await page.locator('.xterm-helper-textarea').focus()
        await page.keyboard.press('F2')
        await page.wait_for_timeout(400)
        start = time.monotonic()
        await page.mouse.click(1660, 845)
        await page.wait_for_function('exportState.actions.chromatograms === false', timeout=750)
        elapsed = time.monotonic() - start
        await page.wait_for_timeout(1300)
        await page.screenshot(path='/tmp/tickyticker-accept-starting.png')
        await page.get_by_role('button', name='Help', exact=True).click()
        await page.wait_for_function('exportState.actions.help === false', timeout=1000)
        await page.keyboard.press('Escape')
        await page.wait_for_function('exportState.ready')
        async with page.expect_download() as pending:
            await page.get_by_role('button', name='Save table', exact=True).click()
        rows = list(csv.DictReader(Path(await (await pending.value).path()).open()))
        assert all(row['QQ'] == '1550' for row in rows)
        assert not errors, errors
        print(f'PASS: manual separator, TIC without fitting, estimated replacement, responsive slow startup; accept response {elapsed*1000:.0f} ms')
        await browser.close()

asyncio.run(main())
