import { chromium } from '/workspace/att_34e54b44ddfd49f2a67d/work/.npm/_npx/6bcb61ec6d5aea22/node_modules/playwright-core/index.mjs'
const tag = process.argv[2] ?? 'before'
const out = process.env.SWARM_ARTIFACTS_DIR
const browser = await chromium.launch()
for (const [w, h] of [[1440, 900], [640, 800], [400, 800]]) {
  const page = await browser.newPage({ viewport: { width: w, height: h } })
  page.on('pageerror', (e) => console.log('pageerror', e.message))
  await page.goto('http://localhost:5173/workflows/wf_0c305fd932ef4239816f')
  await page.waitForSelector('.wf-canvas', { timeout: 20000, state: 'attached' })
  await page.waitForTimeout(800)
  const state = () => page.evaluate(() => {
    const sc = document.querySelector('.ctl-scroll')
    const c = document.querySelector('.wf-canvas').getBoundingClientRect()
    const vm = document.querySelector('.wf-vmap')
    const view = document.querySelector('.wf-vmap .wf-mini-view')
    const fix = [...document.querySelectorAll('[data-step="fix"], .wf-ph-name')].filter((n) => n.getClientRects().length > 0 && (n.dataset.step === 'fix' || n.textContent.trim() === 'fix')).pop()
    return {
      win: { scrollY: window.scrollY, docH: document.documentElement.scrollHeight },
      scroller: sc && { top: sc.scrollTop, sh: sc.scrollHeight, ch: sc.clientHeight },
      canvas: { top: Math.round(c.top), h: Math.round(c.height) },
      vmap: vm ? { label: vm.getAttribute('aria-label').slice(0, 140), viewY: view?.getAttribute('y'), viewH: view?.getAttribute('height'), display: getComputedStyle(vm).display } : null,
      fixTop: fix ? Math.round(fix.getBoundingClientRect().top) : null,
    }
  })
  const portBottom = () => page.evaluate(() => { const r = document.querySelector('.ctl-scroll').getBoundingClientRect(); return r.top + document.querySelector('.ctl-scroll').clientHeight })
  console.log(`== ${w}x${h} initial`, JSON.stringify(await state()))
  await page.screenshot({ path: `${out}/graph-${tag}-${w}x${h}-top.png` })
  // wheel over the canvas
  const cb = (await page.locator('.wf-canvas').boundingBox()) ?? { x: w / 2, y: h / 2, width: 10 }
  await page.mouse.move(Math.max(5, cb.x + Math.min(cb.width, w) / 3), Math.min(Math.max(cb.y + 50, 50), h - 50))
  await page.mouse.wheel(0, 3000)
  await page.waitForTimeout(500)
  console.log(`   after wheel`, JSON.stringify(await state()))
  await page.screenshot({ path: `${out}/graph-${tag}-${w}x${h}-wheel.png` })
  // back to top, then the minimap
  await page.evaluate(() => { const sc = document.querySelector('.ctl-scroll'); sc.scrollTop = 0; window.scrollTo(0, 0) })
  await page.waitForTimeout(200)
  const vm = page.locator('.wf-vmap')
  if (await vm.count() && await vm.isVisible()) {
    const b = await vm.boundingBox()
    await page.mouse.click(b.x + b.width / 2, Math.min(b.y + b.height, await portBottom()) - 3)
    await page.waitForTimeout(300)
    const bb = await vm.boundingBox()
    await page.mouse.click(bb.x + bb.width / 2, Math.min(bb.y + bb.height, await portBottom()) - 3)
    await page.waitForTimeout(500)
    console.log(`   after two minimap clicks at the strip's visible bottom`, JSON.stringify(await state()))
    await page.screenshot({ path: `${out}/graph-${tag}-${w}x${h}-minimap.png` })
    // drag from bottom to top
    const b2 = await vm.boundingBox()
    await page.mouse.move(b2.x + b2.width / 2, Math.min(b2.y + b2.height, await portBottom()) - 3)
    await page.mouse.down()
    await page.mouse.move(b2.x + b2.width / 2, b2.y + 3, { steps: 8 })
    await page.mouse.up()
    await page.waitForTimeout(400)
    console.log(`   after minimap drag (to top)`, JSON.stringify(await state()))
  } else console.log('   no visible minimap')
  // keyboard: focus scroller and press End
  await page.evaluate(() => { document.querySelector('.ctl-scroll').scrollTop = 0 })
  await page.mouse.click(w - 5, Math.round(h / 2))
  await page.keyboard.press('End')
  await page.waitForTimeout(500)
  console.log(`   after End key`, JSON.stringify(await state()))
  await page.close()
}
await browser.close()
