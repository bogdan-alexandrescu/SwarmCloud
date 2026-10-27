async (page) => {
  const AUDIT = `() => {
  const out = [];
  const sectionOf = (el) => { let n = el; while (n) { let p = n.previousElementSibling; while (p) { if (/^H2$/.test(p.tagName)) return p.textContent.trim().slice(0, 60); const h = p.querySelector && p.querySelector('h2'); if (h) return h.textContent.trim().slice(0, 60); p = p.previousElementSibling; } n = n.parentElement; } return '?'; };
  const clipAncestor = (el) => { let a = el.parentElement; while (a && a !== document.body) { const cs = getComputedStyle(a); if (/(hidden|clip)/.test(cs.overflowX) || /(hidden|clip)/.test(cs.overflow)) return a; a = a.parentElement; } return null; };
  const textOf = (el) => (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 50);
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const hasOwnText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1);
    const isControl = /^(BUTTON|A|INPUT|SELECT)$/.test(el.tagName);
    if (!(hasOwnText || isControl)) continue;
    const anc = clipAncestor(el);
    if (anc) { const ar = anc.getBoundingClientRect(); if (anc.scrollWidth > anc.clientWidth + 2 && getComputedStyle(anc).overflowX !== 'auto' && getComputedStyle(anc).overflowX !== 'scroll' && r.right > ar.right + 2) { const key = 'clip|' + textOf(el); if (!seen.has(key)) { seen.add(key); out.push(['clipped', sectionOf(el), textOf(el)]); } } }
    if (hasOwnText && r.width < 40 && r.height > 80 && textOf(el).length > 6) { const key = 'crush|' + textOf(el); if (!seen.has(key)) { seen.add(key); out.push(['crushed', sectionOf(el), textOf(el)]); } }
    const fs = parseFloat(cs.fontSize);
    if (hasOwnText && fs < 11.5) { const key = 'tiny|' + fs + '|' + sectionOf(el); if (!seen.has(key)) { seen.add(key); out.push(['tiny ' + fs + 'px', sectionOf(el), textOf(el)]); } }
  }
  return out;
}`;
  const base = 'http://127.0.0.1:37725/index.html#';
  const res = {};
  for (const r of ['capacity/pools', 'capacity/accounts', 'capacity/quota']) {
    await page.goto(base + r);
    await page.waitForSelector('.c-phead .ctl-q-glyph', { timeout: 8000 });
    await page.waitForTimeout(800);
    const audit = await page.evaluate(`(${AUDIT})()`);
    const info = await page.evaluate(() => ({
      headGlyphs: [...document.querySelectorAll('.c-phead button[aria-label^="Help: "]')].map(b => b.getAttribute('aria-label')),
      footer: document.body.innerText.includes('Reading this screen:'),
      scrollWidth: document.documentElement.scrollWidth,
      dark: matchMedia('(prefers-color-scheme: dark)').matches,
      width: innerWidth,
    }));
    // The set of controls a Tab walk should reach: every visible tabbable in the document.
    const sig = (el) => el ? (el.tagName + '|' + (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g,' ').slice(0, 40) + '|' + (el.getAttribute('href') || '')) : 'null';
    const expected = await page.evaluate(() => {
      const sel = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea,[tabindex]';
      return [...document.querySelectorAll(sel)].filter(el => {
        if (el.tabIndex < 0) return false;
        const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
        if (cs.visibility === 'hidden' || r.width === 0 || r.height === 0) return false;
        if (el.closest('[inert],[aria-hidden="true"]')) return false;
        return true;
      }).map(el => el.tagName + '|' + (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g,' ').slice(0, 40) + '|' + (el.getAttribute('href') || ''));
    });
    await page.evaluate(() => {
      const sel = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea,[tabindex]';
      window.__exp = [...document.querySelectorAll(sel)].filter(el => {
        if (el.tabIndex < 0) return false;
        const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
        if (cs.visibility === 'hidden' || r.width === 0 || r.height === 0) return false;
        return !el.closest('[inert],[aria-hidden="true"]');
      });
      window.__seen = new Set(); window.__last = null; window.__stuck = 0;
      document.activeElement && document.activeElement.blur();
    });
    let steps = 0;
    for (; steps < 600; steps++) {
      await page.keyboard.press('Tab');
      const done = await page.evaluate(() => {
        const el = document.activeElement;
        if (el === window.__last) window.__stuck++;
        window.__last = el; window.__seen.add(el);
        return window.__exp.every(e => window.__seen.has(e) || !e.isConnected);
      });
      if (done) break;
    }
    const walk = await page.evaluate(() => ({ stuck: window.__stuck, missing: window.__exp.filter(e => e.isConnected && !window.__seen.has(e)).map(e => e.tagName + '|' + (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 40)) }));
    const reached = { size: 0 }; const swallowed = walk.stuck; const order = { length: steps + 1 };
    const missing = walk.missing;
    // The card's topic links, reached by Tab from the head ?.
    await page.focus('.c-phead .ctl-q-glyph');
    const cardLinks = await page.evaluate(() => [...document.querySelectorAll('.ctl-q-card a[href^="#help/"]')].length);
    await page.keyboard.press('Tab');
    const intoCard = await page.evaluate(() => !!document.activeElement.closest('.ctl-q-card'));
    await page.keyboard.press('Escape');
    res[r] = { ...info, audit, expected: expected.length, reached: reached.size, missing, swallowed, cardLinks, intoCard, steps: order.length };
  }
  return res;
}
