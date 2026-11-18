// Rendered-pixel overflow / clipping / overlap probe.
//
// Deliberately conservative. Every exclusion in the brief is applied by
// construction, and each finding carries the computed values that caused it so
// a reader can re-derive it without running this again.
(() => {
  const scan = () => {
  const VW = window.innerWidth
  const VH = window.innerHeight
  const out = []

  const sel = (el) => {
    if (!el || el.nodeType !== 1) return String(el)
    let s = el.tagName.toLowerCase()
    if (el.id) s += '#' + el.id
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean)
    if (cls.length) s += '.' + cls.slice(0, 3).join('.')
    // position among same-tag siblings, to make it findable
    const p = el.parentElement
    if (p) {
      const same = Array.from(p.children).filter((c) => c.tagName === el.tagName)
      if (same.length > 1) s += ':nth(' + (same.indexOf(el) + 1) + ')'
    }
    return s
  }
  const path = (el) => {
    const parts = []
    let n = el
    while (n && n.nodeType === 1 && parts.length < 5) {
      parts.unshift(sel(n))
      n = n.parentElement
    }
    return parts.join(' > ')
  }
  const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90)
  const r2 = (n) => Math.round(n * 10) / 10

  const all = Array.from(document.querySelectorAll('body *'))

  const hidden = (el) => {
    if (el.closest('[aria-hidden="true"]')) return true
    if (el.closest('details:not([open])') && el.closest('summary') === null) return true
    let n = el
    while (n && n.nodeType === 1) {
      const cs = getComputedStyle(n)
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') return true
      if (parseFloat(cs.opacity) === 0) return true
      n = n.parentElement
    }
    return false
  }

  // ---- pass 1: gather visible boxes ----------------------------------------
  const vis = []
  for (const el of all) {
    const tag = el.tagName
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK' || tag === 'BR' || tag === 'DEFS') continue
    if (el.closest('svg') && el.tagName !== 'svg') continue // svg internals: not layout
    const rect = el.getBoundingClientRect()
    if (rect.width < 2 || rect.height < 2) continue
    if (el.clientWidth < 2 && el.clientHeight < 2 && !(el.tagName === 'SPAN' || el.tagName === 'A')) continue
    if (hidden(el)) continue
    vis.push({ el, rect, cs: getComputedStyle(el) })
  }

  // An element scrolled out of its own scroll container is NOT covered and is
  // NOT clipped: it is off-screen content the viewer reaches by scrolling.
  // Verified 2026-09-23: without this, every `covered` hit on this app was the
  // static dock grid row standing where scrolled-away content's rect landed.
  const outOfOwnScroller = (el, r) => {
    let p = el.parentElement
    while (p && p.nodeType === 1) {
      const cs = getComputedStyle(p)
      const clips = cs.overflowX !== 'visible' || cs.overflowY !== 'visible'
      if (clips) {
        const pr = p.getBoundingClientRect()
        if (r.bottom < pr.top - 1 || r.top > pr.bottom + 1) return true
        if (r.right < pr.left - 1 || r.left > pr.right + 1) return true
      }
      p = p.parentElement
    }
    return false
  }

  // The part of a rect that its clipping ancestors actually let through. A
  // point sampled outside this is a point the element does not paint, so
  // hit-testing there answers a question nobody asked.
  const visibleRect = (el, r) => {
    let L = r.left, T = r.top, R = r.right, B = r.bottom
    let p = el.parentElement
    while (p && p.nodeType === 1) {
      const cs = getComputedStyle(p)
      if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
        const pr = p.getBoundingClientRect()
        L = Math.max(L, pr.left); T = Math.max(T, pr.top)
        R = Math.min(R, pr.right); B = Math.min(B, pr.bottom)
      }
      p = p.parentElement
    }
    L = Math.max(L, 0); T = Math.max(T, 0)
    R = Math.min(R, window.innerWidth); B = Math.min(B, window.innerHeight)
    return { left: L, top: T, right: R, bottom: B, width: R - L, height: B - T }
  }

  const scrollable = (cs, el, axis) => {
    const o = axis === 'x' ? cs.overflowX : cs.overflowY
    if (o !== 'auto' && o !== 'scroll') return false
    // a real scrollbar / real scroll range
    return axis === 'x' ? el.scrollWidth > el.clientWidth : el.scrollHeight > el.clientHeight
  }

  // ---- A. self-clipping: content cut off by overflow hidden/clip ------------
  for (const { el, rect, cs } of vis) {
    for (const axis of ['x', 'y']) {
      const o = axis === 'x' ? cs.overflowX : cs.overflowY
      if (o !== 'hidden' && o !== 'clip') continue
      const s = axis === 'x' ? el.scrollWidth : el.scrollHeight
      const c = axis === 'x' ? el.clientWidth : el.clientHeight
      const over = s - c
      if (over <= 1) continue
      // ellipsis is intended design on a single-line x clip
      if (axis === 'x' && cs.textOverflow === 'ellipsis') continue
      out.push({
        kind: 'clip',
        axis,
        el: path(el),
        text: txt(el),
        over: r2(over),
        client: c,
        scroll: s,
        css: `overflow-${axis}:${o} text-overflow:${cs.textOverflow} white-space:${cs.whiteSpace}`,
        rect: [r2(rect.x), r2(rect.y), r2(rect.width), r2(rect.height)],
      })
    }
  }

  // ---- B. escaping an ancestor's padding box (ancestor does NOT clip) ------
  for (const { el, rect, cs } of vis) {
    if (cs.position === 'fixed') continue
    if (outOfOwnScroller(el, rect)) continue
    let p = el.parentElement
    let depth = 0
    while (p && p !== document.body && depth < 3) {
      const pcs = getComputedStyle(p)
      const pr = p.getBoundingClientRect()
      const clipsX = pcs.overflowX !== 'visible'
      const clipsY = pcs.overflowY !== 'visible'
      const padL = parseFloat(pcs.paddingLeft) || 0
      const padR = parseFloat(pcs.paddingRight) || 0
      const dx = Math.max(pr.left + padL - rect.left, rect.right - (pr.right - padR))
      const dy = Math.max(pr.top - rect.top, rect.bottom - pr.bottom)
      if (!clipsX && dx > 2 && pcs.position !== 'static' === false) {
        // only report when it actually leaves the box, not merely the padding
        if (rect.right - pr.right > 2 || pr.left - rect.left > 2) {
          out.push({
            kind: 'escape-x',
            el: path(el),
            text: txt(el),
            parent: path(p),
            by: r2(Math.max(rect.right - pr.right, pr.left - rect.left)),
            css: `parent overflow-x:${pcs.overflowX} child width:${r2(rect.width)} parent inner:${r2(pr.width - padL - padR)}`,
            rect: [r2(rect.x), r2(rect.y), r2(rect.width), r2(rect.height)],
          })
        }
      }
      const isPageRoot = p === document.body || p.id === 'root' || p === document.documentElement
      if (!clipsY && !isPageRoot && rect.bottom - pr.bottom > 2 && pcs.display !== 'inline') {
        const last = p.lastElementChild
        if (el === last || el.contains(last) || (last && last.contains(el))) {
          // last child spilling past parent bottom is the interesting case
          out.push({
            kind: 'escape-y',
            el: path(el),
            text: txt(el),
            parent: path(p),
            by: r2(rect.bottom - pr.bottom),
            css: `parent overflow-y:${pcs.overflowY} parent h:${r2(pr.height)}`,
            rect: [r2(rect.x), r2(rect.y), r2(rect.width), r2(rect.height)],
          })
        }
      }
      if (clipsX || clipsY) break
      p = p.parentElement
      depth++
    }
  }

  // ---- B2. a scroll container with no rendered scrollbar --------------------
  // overflow:auto is excluded by the brief when there is a REAL scrollbar. On
  // this platform the scrollbars are overlay: the gutter is 0px and nothing is
  // painted until a scroll begins, so the content is simply not there for the
  // viewer. Reported apart from the hard clips, and never mixed with them.
  for (const { el, rect, cs } of vis) {
    const ox = cs.overflowX
    if (ox !== 'auto' && ox !== 'scroll') continue
    const hidden = el.scrollWidth - el.clientWidth
    if (hidden <= 2) continue
    const gutter = el.offsetHeight - el.clientHeight - (parseFloat(cs.borderTopWidth) || 0) - (parseFloat(cs.borderBottomWidth) || 0)
    out.push({
      kind: 'no-affordance-x',
      el: path(el),
      text: txt(el),
      hiddenPx: r2(hidden),
      hiddenPct: Math.round((hidden / el.scrollWidth) * 100),
      scrollbarGutterPx: r2(gutter),
      css: `overflow-x:${ox} client:${el.clientWidth} scroll:${el.scrollWidth}`,
      rect: [r2(rect.x), r2(rect.y), r2(rect.width), r2(rect.height)],
    })
  }

  // ---- C. out of the viewport horizontally --------------------------------
  const docOver = document.documentElement.scrollWidth - document.documentElement.clientWidth
  for (const { el, rect, cs } of vis) {
    if (rect.right > VW + 1 || rect.left < -1) {
      // ignore things whose nearest scroll container legitimately scrolls
      let p = el.parentElement
      let inScroller = false
      while (p && p !== document.body) {
        const pcs = getComputedStyle(p)
        if (scrollable(pcs, p, 'x')) { inScroller = true; break }
        if (pcs.overflowX === 'hidden' || pcs.overflowX === 'clip') { inScroller = true; break }
        p = p.parentElement
      }
      if (inScroller) continue
      out.push({
        kind: 'offscreen-x',
        el: path(el),
        text: txt(el),
        by: r2(Math.max(rect.right - VW, -rect.left)),
        css: `position:${cs.position} width:${r2(rect.width)} docScrollOver:${docOver}`,
        rect: [r2(rect.x), r2(rect.y), r2(rect.width), r2(rect.height)],
      })
    }
  }

  // ---- D. covered: a text-bearing box whose own pixels are hit by another ---
  const textLeaves = vis.filter(({ el }) => {
    if (!el.firstChild) return false
    let hasText = false
    for (const n of el.childNodes) if (n.nodeType === 3 && n.nodeValue.trim()) hasText = true
    return hasText
  })
  const opaque = (el) => {
    let n = el
    while (n && n.nodeType === 1) {
      const cs = getComputedStyle(n)
      const bg = cs.backgroundColor
      const m = bg.match(/rgba?\(([^)]+)\)/)
      if (m) {
        const p = m[1].split(',').map((s) => parseFloat(s))
        if (p.length < 4 || p[3] > 0.15) return n
      }
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return n
      if (cs.backdropFilter && cs.backdropFilter !== 'none') return n
      n = n.parentElement
    }
    return null
  }
  for (const { el, rect } of textLeaves) {
    if (outOfOwnScroller(el, rect)) continue
    const rects = Array.from(el.getClientRects()).filter((r) => r.width > 2 && r.height > 2)
    if (!rects.length) continue
    for (const raw of rects.slice(0, 3)) {
      const r = visibleRect(el, raw)
      if (r.width < 3 || r.height < 3) continue
      const pts = [
        [r.left + Math.min(8, r.width / 2), r.top + r.height / 2],
        [r.left + r.width / 2, r.top + r.height / 2],
        [r.right - Math.min(8, r.width / 2), r.top + r.height / 2],
      ]
      let covered = null
      for (const [x, y] of pts) {
        if (x < 0 || y < 0 || x > VW || y > VH) continue
        const hit = document.elementFromPoint(x, y)
        if (!hit) continue
        if (hit === el || el.contains(hit) || hit.contains(el)) continue
        const op = opaque(hit)
        covered = { hit, op, x, y }
        break
      }
      if (covered) {
        let anc = el, viaEllipsis = false
        while (anc && anc.nodeType === 1) {
          const acs = getComputedStyle(anc)
          if (acs.textOverflow === 'ellipsis' && anc.scrollWidth - anc.clientWidth > 1) { viaEllipsis = true; break }
          anc = anc.parentElement
        }
        if (viaEllipsis) continue
        const hcs = getComputedStyle(covered.hit)
        out.push({
          kind: covered.op ? 'covered' : 'pointer-blocked',
          el: path(el),
          text: txt(el),
          by: path(covered.hit),
          byText: txt(covered.hit),
          at: [r2(covered.x), r2(covered.y)],
          css: `hit position:${hcs.position} z:${hcs.zIndex} bg:${hcs.backgroundColor}`,
          rect: [r2(r.left), r2(r.top), r2(r.width), r2(r.height)],
        })
        break
      }
    }
  }

  // ---- E. ellipsis that is actually eating content -------------------------
  for (const { el, cs, rect } of vis) {
    if (cs.textOverflow !== 'ellipsis') continue
    if (outOfOwnScroller(el, rect)) continue
    const cut = el.scrollWidth - el.clientWidth
    if (cut <= 1) continue
    out.push({
      kind: 'ellipsis',
      el: path(el),
      text: txt(el),
      cut: r2(cut),
      lostPct: Math.round((cut / el.scrollWidth) * 100),
      client: el.clientWidth,
      scroll: el.scrollWidth,
      css: `width:${cs.width} max-width:${cs.maxWidth} flex:${cs.flex}`,
      rect: [r2(rect.x), r2(rect.y), r2(rect.width), r2(rect.height)],
    })
  }

  return { docScrollX: docOver, findings: out }
  }

  // THE PAGE SCROLLS, SO ONE SCAN SEES ONE SCREENFUL. Scan at every screenful
  // and union by identity: a defect two screens down is still a defect, and a
  // scan that only ever looked at the top is how the previous probe missed
  // most of the app while over-reporting the part it could see.
  const scroller = [document.scrollingElement, ...document.querySelectorAll('*')]
    .filter((e) => e && e.scrollHeight - e.clientHeight > 4)
    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0]
  const step = scroller ? Math.max(200, scroller.clientHeight - 80) : 0
  const stops = []
  if (scroller) for (let y = 0; y <= scroller.scrollHeight - scroller.clientHeight + step; y += step) stops.push(y)
  else stops.push(0)

  const seen = new Map()
  // Synchronous on purpose: assigning scrollTop forces layout in Chrome, so the
  // very next getBoundingClientRect/elementFromPoint already reflects the new
  // scroll position. `bb eval` does not await a Promise, and a scan that
  // silently returned nothing is worse than one that blocks for 200ms.
  let docScrollX = 0
  for (const y of stops) {
    if (scroller) scroller.scrollTop = y
    void document.documentElement.offsetHeight
    const res = scan()
    docScrollX = Math.max(docScrollX, res.docScrollX)
    for (const f of res.findings) {
      const k = [f.kind, f.el, f.text, f.axis || '', f.by || '', f.parent || ''].join('|')
      if (!seen.has(k)) seen.set(k, { ...f, seenAtScrollY: y })
    }
  }
  if (scroller) scroller.scrollTop = 0
  const findings = Array.from(seen.values())
  return {
    url: location.hash,
    vw: window.innerWidth,
    vh: window.innerHeight,
    scrollStops: stops.length,
    scrollHeight: scroller ? scroller.scrollHeight : 0,
    docScrollX,
    theme: document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light(system)' : 'dark(system)'),
    n: findings.length,
    findings,
  }
})()
