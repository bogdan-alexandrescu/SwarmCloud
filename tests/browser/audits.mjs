// The in-page audits, as source strings evaluated in the real page.
//
// Each returns an array of findings. A finding is:
//
//   { category, kind, selector, detail, text }
//
// The runner adds `view` and `viewport` and turns the four of
// (view, viewport, kind, selector) into the finding's KEY -- the thing compared
// against the baseline. `detail` and `text` are evidence for a human and are
// deliberately NOT part of the key, so one more clipped row in a table is the
// same finding rather than a new one, while a new KIND of element clipping is
// a new finding.
//
// Everything here runs against computed style and layout boxes rather than
// pixels. A screenshot diff would fail on a font hinting change and pass on a
// URL squeezed from 298px into 158px, which is the failure this suite exists
// for.

/** Shared helpers, prepended to every audit. */
const PRELUDE = `
  const TOL = 1;

  function selectorFor(el) {
    if (!el || el === document.body) return 'body';
    const tag = el.tagName.toLowerCase();
    const id = el.id ? '#' + el.id : '';
    const cls = (typeof el.className === 'string' && el.className.trim())
      ? '.' + el.className.trim().split(/\\s+/).slice(0, 4).join('.')
      : '';
    return tag + id + cls;
  }

  function pathFor(el) {
    const parts = [];
    let cur = el;
    for (let i = 0; cur && cur !== document.body && i < 4; i++) {
      parts.unshift(selectorFor(cur));
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function visible(el) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (Number(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  /** Screen-reader-only text is meant to be off-box. Not a layout defect. */
  function srOnly(el) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return (r.width <= 1 && r.height <= 1) || cs.clipPath === 'inset(50%)' || cs.clip === 'rect(0px, 0px, 0px, 0px)';
  }

  function ownText(el) {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.trim();
  }

  function srgbToLinear(c) {
    c = c / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }

  function luminance(rgb) {
    return 0.2126 * srgbToLinear(rgb[0]) + 0.7152 * srgbToLinear(rgb[1]) + 0.0722 * srgbToLinear(rgb[2]);
  }

  function ratio(a, b) {
    const la = luminance(a), lb = luminance(b);
    const hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }

  function parseColor(s) {
    const m = /rgba?\\(([^)]+)\\)/.exec(s || '');
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return { rgb: [p[0], p[1], p[2]], a: p.length > 3 ? p[3] : 1 };
  }

  function over(fg, bg) {
    // Composite a partly transparent foreground onto an opaque background.
    return [0, 1, 2].map(i => Math.round(fg.rgb[i] * fg.a + bg[i] * (1 - fg.a)));
  }

  /** The first opaque background behind this element, composited downwards. */
  function effectiveBackground(el) {
    const stack = [];
    let cur = el;
    while (cur) {
      const c = parseColor(getComputedStyle(cur).backgroundColor);
      if (c && c.a > 0) {
        stack.push(c);
        if (c.a >= 0.999) break;
      }
      cur = cur.parentElement;
    }
    // Nothing opaque found: the canvas. Read it off <html> rather than
    // assuming white -- this product ships a dark theme and an assumed white
    // page would report every body colour as a contrast failure.
    let base = [255, 255, 255];
    const html = parseColor(getComputedStyle(document.documentElement).backgroundColor);
    if (html && html.a >= 0.999) base = html.rgb;
    for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
    return base;
  }
`

/**
 * THE ONE AUDIT CALL.
 *
 * Four categories in a single evaluation, because each navigation costs about
 * a second and a half of settle time and running them separately would
 * quadruple a matrix run for no extra signal. Honesty and keyboard are not
 * here: both need the page driven (a scenario switched, a key pressed), so
 * they are the runner's business.
 */
export const PAGE_AUDIT = `
  ${PRELUDE}
  const out = [];
  const W = document.documentElement.clientWidth;
  const push = (category, kind, el, detail, text) => out.push({
    category, kind,
    selector: el ? selectorFor(el) : 'document',
    path: el ? pathFor(el) : 'document',
    detail: String(detail),
    text: String(text || '').replace(/\\s+/g, ' ').slice(0, 140),
  });

  // ---------------------------------------------------------------- layout
  // A document that scrolls sideways is the coarsest form of the defect and
  // the one a person notices last, because the scrollbar is at the bottom of
  // a long page.
  if (document.documentElement.scrollWidth > W + TOL) {
    push('layout', 'doc-hscroll', null,
      'documentElement.scrollWidth=' + document.documentElement.scrollWidth + ' clientWidth=' + W,
      'the page scrolls sideways');
  }

  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el) || srOnly(el)) continue;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();

    // 1. Past the right edge. Only counted when the element STARTS on screen
    //    (r.left < W): a drawer parked off-canvas is not clipped, it is shut.
    //    Ancestors that can be scrolled sideways are excluded too -- content
    //    you can reach by scrolling is reachable.
    if (r.right > W + TOL && r.left < W) {
      let scrollable = false;
      for (let p = el.parentElement; p; p = p.parentElement) {
        const pc = getComputedStyle(p);
        if (pc.overflowX === 'auto' || pc.overflowX === 'scroll') { scrollable = true; break; }
      }
      if (!scrollable) {
        push('layout', 'overflow-right', el,
          'right=' + Math.round(r.right) + ' viewport=' + W,
          ownText(el) || el.textContent);
      }
    }

    // 2. Content wider than the box, with the overflow HIDDEN. Nothing the
    //    reader can do reaches it. This is the 16-per-view finding in
    //    docs/web-ui/evidence/: URLs squeezed from ~300px into 158px.
    if (cs.overflowX === 'hidden' && el.scrollWidth > el.clientWidth + TOL) {
      push('layout', 'clipped-x', el,
        'scrollWidth=' + el.scrollWidth + ' clientWidth=' + el.clientWidth,
        ownText(el) || el.textContent);
    }

    // 3. Ellipsised. Distinct from (2) on purpose: an ellipsis is a deliberate
    //    choice that still LOSES information, and a route or an id truncated
    //    in the middle cannot be copied, read out, or searched for.
    if (cs.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + TOL) {
      push('layout', 'text-truncated', el,
        el.scrollWidth + '>' + el.clientWidth,
        ownText(el) || el.textContent);
    }
  }

  // ----------------------------------------------------------- identifiers
  // A displayed id that differs from the real one is unusable: it cannot be
  // pasted into a query, an API call or a log search. QuotaDetail.tsx already
  // documents that reasoning; this is that reasoning made enforceable.
  //
  // The check is not "is text-transform set" but "does this transform CHANGE
  // the id". A text-transform of lowercase over an already-lowercase id alters
  // nothing and is not a defect, and a check that flagged it would be noise
  // the first person to read the report would learn to skip.
  const ID_RE = /\\b(task|tsk|wf|att|lease|evt)_[A-Za-z0-9._-]{4,}/g;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seenIds = new Set();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const raw = n.nodeValue || '';
    const ids = raw.match(ID_RE);
    if (!ids) continue;
    const el = n.parentElement;
    if (!el || !visible(el)) continue;
    const tt = getComputedStyle(el).textTransform;
    if (tt === 'none') continue;
    for (const id of ids) {
      let shown = id;
      if (tt === 'uppercase') shown = id.toUpperCase();
      else if (tt === 'lowercase') shown = id.toLowerCase();
      else if (tt === 'capitalize') shown = id.replace(/\\b\\w/g, c => c.toUpperCase());
      if (shown === id) continue;
      const key = selectorFor(el) + '|' + tt;
      if (seenIds.has(key)) continue;
      seenIds.add(key);
      push('identifiers', 'id-transformed', el,
        'text-transform:' + tt + ' renders ' + id + ' as ' + shown,
        id);
    }
  }

  // -------------------------------------------------------------- headings
  // The nav label, the selected tab and the h1 are three names for one place.
  // When they disagree the reader has to hold a translation table: nav Pools
  // shows h1 Capacity, nav History shows h1 Activity.
  const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  const h1 = document.querySelector('h1');
  const navOn = document.querySelector('.ctl-nav-link[aria-current="page"], .ctl-nav-util button[aria-current="page"]');
  const tabOn = document.querySelector('[role="tab"][aria-selected="true"]');
  if (h1) {
    const heading = norm(h1.textContent);
    const navLabel = navOn ? norm(navOn.textContent) : null;
    // The tab strip renders an "admin" marker inside the button; strip it, or
    // every admin tab reads as a mismatch it is not.
    const tabLabel = tabOn ? norm(String(tabOn.textContent).replace(/admin$/i, '')) : null;
    const agrees = (navLabel !== null && navLabel === heading) || (tabLabel !== null && tabLabel === heading);
    if (!agrees) {
      push('headings', 'nav-heading-disagree', h1,
        'h1="' + String(h1.textContent).trim() + '" nav="' + (navOn ? navOn.textContent.trim() : '-') +
        '" tab="' + (tabOn ? tabOn.textContent.trim() : '-') + '"',
        h1.textContent);
    }
  } else {
    push('headings', 'no-h1', null, 'this view renders no h1 at all', '');
  }

  // -------------------------------------------------------------- contrast
  // WCAG AA over the COMPOSITED background, not over an assumed white page.
  const seenContrast = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el) || srOnly(el)) continue;
    const t = ownText(el);
    if (!t) continue;
    // DISABLED CONTROLS ARE EXEMPT, and this is WCAG's own carve-out rather
    // than a convenience: 1.4.3 excludes "inactive user interface components",
    // because dimming is how a control SAYS it is inactive. Reporting them
    // would have filed the submit button on the New agent form -- greyed out
    // until the form is valid, exactly as intended -- as an accessibility
    // defect, and four such lines is how a reader learns to skim this section.
    if (el.disabled === true || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest('[disabled], [aria-disabled="true"], fieldset:disabled')) continue;
    const cs = getComputedStyle(el);
    const fg = parseColor(cs.color);
    if (!fg) continue;
    const bg = effectiveBackground(el);
    const composited = fg.a >= 0.999 ? fg.rgb : over(fg, bg);
    const size = parseFloat(cs.fontSize);
    const weight = parseInt(cs.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3.0 : 4.5;
    const got = ratio(composited, bg);
    if (got + 0.01 < need) {
      const key = selectorFor(el) + '|' + cs.color + '|' + Math.round(size);
      if (seenContrast.has(key)) continue;
      seenContrast.add(key);
      push('contrast', large ? 'contrast-large' : 'contrast-body', el,
        got.toFixed(2) + ':1 needs ' + need.toFixed(1) + ':1 at ' + size + 'px/' + weight +
        ' (fg ' + cs.color + ' on rgb(' + bg.join(',') + '))',
        t);
    }
  }

  // ------------------------------------------- state legible in greyscale
  // These states are the difference between running and dead. A pair of them
  // separated only by hue is a pair a colour-blind reader, a greyscale print
  // and a screenshot in a dark terminal all render identically.
  //
  // Hue alone is not the failure -- hue alone WITHOUT a second signal is. A
  // chip that also carries a distinct glyph, or a hollow dot instead of a
  // filled one, survives the conversion and is not reported.
  //
  // TWO THINGS THIS GETS WRONG IF WRITTEN THE OBVIOUS WAY, both found by
  // running it:
  //
  // 1. THE TONE CLASS IS OFTEN NOT ON THE COLOURED ELEMENT. '.ctl-metric.is-good'
  //    colours '.ctl-metric-value', a CHILD. Reading getComputedStyle(el).color
  //    on the element carrying the class returns the inherited body colour for
  //    every tone, so every pair looks identical and the check reports 36
  //    collisions that do not exist. The tint is therefore hunted for: the
  //    element itself if it is tinted, otherwise the first descendant that is.
  //    A tone with no tint anywhere is not expressed as colour at all and has
  //    nothing to collide with, so it is skipped rather than reported.
  //
  // 2. A BORDER IS A NON-COLOUR MARK. '.ctl-metric.is-absent' is drawn with a
  //    dashed border, which survives greyscale perfectly well. The mark must
  //    include the element's own border-style, not only a child dot's.
  const neutral = parseColor(getComputedStyle(document.body).color);
  const sameColour = (a, b) => a && b && a.rgb[0] === b.rgb[0] && a.rgb[1] === b.rgb[1] && a.rgb[2] === b.rgb[2];

  /**
   * EVERY colour a tone paints, as a set of luminances.
   *
   * Not "the colour of the element carrying the class", which is what the
   * obvious version reads and what made this check useless twice over:
   *
   *   1. The tone class is usually not on the coloured element. A rule like
   *      .ctl-metric.is-good .ctl-metric-value colours a CHILD, so the element
   *      with the class inherits the body colour and every tone reads as
   *      identical -- 36 collisions reported where none existed.
   *   2. Taking the first tinted descendant instead is no better: the first
   *      tinted child of every tile is its LABEL, which is the same faint grey
   *      for all of them. Same false positive, four times instead of thirty-six.
   *
   * A tone is a whole treatment, so the comparison is between whole
   * treatments: the set of luminances the subtree paints, plus the border
   * colour, rounded so that two identical palettes compare equal. Two tones
   * collide only when those sets are indistinguishable in greyscale AND they
   * carry no other mark.
   */
  function paletteOf(el) {
    const out = new Set();
    const consider = (colorStr) => {
      const c = parseColor(colorStr);
      if (!c || c.a < 0.05) return;
      if (sameColour(c, neutral)) return;
      out.add(luminance(c.rgb));
    };
    const cs = getComputedStyle(el);
    if (ownText(el)) consider(cs.color);
    consider(cs.borderTopColor);
    for (const kid of el.querySelectorAll('*')) {
      if (!visible(kid)) continue;
      const kcs = getComputedStyle(kid);
      if (ownText(kid)) consider(kcs.color);
      consider(kcs.borderTopColor);
      consider(kcs.backgroundColor);
    }
    return [...out].sort((x, y) => x - y);
  }

  const tones = new Map();
  for (const el of document.querySelectorAll('[class*="is-"], .st')) {
    if (!visible(el)) continue;
    const tone = (String(el.className).match(/\\bis-(ok|warn|bad|info|paused|unknown|live|good|alert|absent)\\b/) || [])[1];
    if (!tone) continue;
    const palette = paletteOf(el);
    // A tone that paints nothing of its own is not expressed as colour at all,
    // so it has nothing to collide with.
    if (palette.length === 0) continue;
    const cs = getComputedStyle(el);
    const dot = el.querySelector('i');
    const glyph = el.querySelector('[aria-hidden="true"]');
    const mark = [
      cs.borderStyle,
      cs.textDecorationLine,
      cs.fontWeight,
      dot ? getComputedStyle(dot).borderStyle + '/' + getComputedStyle(dot).backgroundColor : 'nodot',
      glyph ? String(glyph.textContent).trim() : 'noglyph',
    ].join('|');
    if (!tones.has(tone)) tones.set(tone, { palette, mark, selector: selectorFor(el) });
  }
  // ONLY THE COLOURS THE TONE IS RESPONSIBLE FOR.
  //
  // Comparing whole subtree palettes was still wrong, and the mutation test is
  // what said so: every tile shares its label grey, its sub-line grey and its
  // foot grey, so a comparison that accepted "some colour matches" reported
  // is-good against is-alert at 1.000:1 -- on the shared LABEL, while the
  // values were plainly green and red.
  //
  // So the colours common to every tone on the page are subtracted first. What
  // is left is what the tone class actually contributes, and that is what gets
  // compared. A tone with nothing left contributes no colour of its own and is
  // skipped rather than reported.
  const toneList = [...tones.entries()];
  let shared = null;
  for (const [, t] of toneList) {
    if (shared === null) { shared = new Set(t.palette); continue; }
    shared = new Set([...shared].filter((l) => t.palette.includes(l)));
  }
  shared = shared ?? new Set();
  for (const [, t] of toneList) {
    t.distinctive = t.palette.filter((l) => !shared.has(l)).sort((x, y) => x - y);
  }

  for (let i = 0; i < toneList.length; i++) {
    for (let j = i + 1; j < toneList.length; j++) {
      const [na, a] = toneList[i], [nb, b] = toneList[j];
      if (a.mark !== b.mark) continue;
      if (a.distinctive.length === 0 || b.distinctive.length === 0) continue;
      if (a.distinctive.length !== b.distinctive.length) continue;
      // EVERY distinctive colour must match, not merely one of them -- the
      // widest gap decides, never the narrowest. 1.2:1 is the point below
      // which two greys stop being two greys, on a screen, in print, or in a
      // screenshot somebody pasted into a terminal.
      let widest = 1;
      for (let k = 0; k < a.distinctive.length; k++) {
        const hi = Math.max(a.distinctive[k], b.distinctive[k]);
        const lo = Math.min(a.distinctive[k], b.distinctive[k]);
        widest = Math.max(widest, (hi + 0.05) / (lo + 0.05));
      }
      if (widest >= 1.2) continue;
      push('contrast', 'greyscale-collision', null,
        'is-' + na + ' and is-' + nb + ' are distinguished by hue alone: their own colours sit within ' +
        widest.toFixed(3) + ':1 in luminance and they carry the same non-colour mark (' + a.mark + ')',
        na + ' vs ' + nb);
    }
  }

  return out;
`

/** Everything the honesty assertions read. Collected, never judged, in-page. */
export const HONESTY_SNAPSHOT = `
  ${PRELUDE}
  const numeric = s => /^[\\s$£€]*-?\\d[\\d,.]*\\s*%?\\s*$/.test(String(s || '').trim());
  const metrics = [...document.querySelectorAll('.ctl-metric-value')].map(e => ({
    label: (e.parentElement.querySelector('.ctl-metric-label') || {}).textContent || '',
    value: String(e.textContent || '').replace(/\\s+/g, ' ').trim(),
    sub: (e.parentElement.querySelector('.ctl-metric-sub') || {}).textContent || '',
    foot: (e.parentElement.querySelector('.ctl-metric-foot') || {}).textContent || '',
    selector: selectorFor(e),
  }));
  // Every element whose whole visible text is a bare number. This is the set
  // that must be EMPTY inside a failed region: a zero rendered for a read that
  // did not happen is the defect this product was built to stop.
  const bareNumbers = [];
  for (const el of document.querySelectorAll('.ctl-metric-value, td, .n, .ctl-util-figure, .ov-tile *')) {
    if (!visible(el)) continue;
    const t = ownText(el);
    if (t && numeric(t)) bareNumbers.push({ selector: selectorFor(el), path: pathFor(el), text: t });
  }
  return {
    h1: (document.querySelector('h1') || {}).textContent || null,
    sub: String((document.querySelector('.sub') || {}).textContent || '').replace(/\\s+/g, ' ').trim(),
    metrics,
    bareNumbers,
    panels: [...document.querySelectorAll('.state')].map(e => ({
      cls: e.className,
      text: String(e.innerText || '').replace(/\\s+/g, ' ').trim(),
    })),
    probes: [...document.querySelectorAll('.source')].map(e => ({
      cls: e.className,
      path: String((e.querySelector('.s-path') || {}).textContent || ''),
      status: String((e.querySelector('.s-status') || {}).textContent || ''),
    })),
    emDashes: (String(document.body.innerText || '').match(/\\u2014/g) || []).length,
    text: String(document.body.innerText || '').replace(/\\s+/g, ' ').trim(),
  };
`

/** Tag every interactive control so a Tab walk can name what it reached. */
export const MARK_INTERACTIVE = `
  ${PRELUDE}
  const SEL = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), ' +
              'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const all = [...document.querySelectorAll(SEL)].filter(visible);
  all.forEach((el, i) => el.setAttribute('data-uitest', String(i)));
  window.__uitestFocus = [];
  return all.map((el, i) => ({
    idx: i,
    selector: selectorFor(el),
    path: pathFor(el),
    label: String(el.getAttribute('aria-label') || el.textContent || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
  }));
`

/**
 * Record where focus landed, and whether anything on screen says so.
 *
 * A focus ring is the only thing telling a keyboard user where they are. It
 * counts if it is an outline OR a box-shadow OR a border the element does not
 * otherwise have -- the check is "is there a visible change", not "did you use
 * the property I expected".
 */
export const RECORD_FOCUS = `
  ${PRELUDE}
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {
    window.__uitestFocus.push({ idx: null, selector: 'body', ring: null });
    return 'body';
  }
  const cs = getComputedStyle(el);
  const ring =
    (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0) ||
    (cs.boxShadow && cs.boxShadow !== 'none');
  window.__uitestFocus.push({
    idx: el.hasAttribute('data-uitest') ? Number(el.getAttribute('data-uitest')) : null,
    selector: selectorFor(el),
    path: pathFor(el),
    label: String(el.getAttribute('aria-label') || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
    ring: !!ring,
    ringDetail: cs.outlineStyle + ' ' + cs.outlineWidth + ' / shadow:' + (cs.boxShadow || 'none').slice(0, 40),
    inDialog: !!el.closest('[role="dialog"]'),
  });
  return 'ok';
`

export const READ_FOCUS_TRACE = `return window.__uitestFocus || [];`

// ---------------------------------------------------------------------------
// The guard on this file itself
// ---------------------------------------------------------------------------
//
// EVERY AUDIT ABOVE IS A TEMPLATE LITERAL, AND A BACKTICK IN A COMMENT INSIDE
// ONE ENDS IT. That has happened twice while writing this file, both times in a
// comment quoting a CSS selector. The failure is silent in the worst way: the
// literal closes early, the remaining lines become JavaScript in the module
// scope, and the error that surfaces is whatever those lines happen to mean --
// "SyntaxError: Unexpected identifier 'text'", "ReferenceError: metric is not
// defined". Neither says "your comment has a backtick in it", and neither
// points at the audit that is now half a script.
//
// So each source is compiled here, at import, as the function body it is about
// to be used as. A truncated audit fails immediately with its own name on it,
// instead of failing in a browser twenty minutes into a run.
for (const [name, source] of Object.entries({
  PAGE_AUDIT,
  HONESTY_SNAPSHOT,
  MARK_INTERACTIVE,
  RECORD_FOCUS,
  READ_FOCUS_TRACE,
})) {
  try {
    // eslint-disable-next-line no-new-func
    new Function(source)
  } catch (err) {
    throw new SyntaxError(
      `tests/browser/audits.mjs: ${name} is not a valid function body (${err.message}). ` +
        'The usual cause is a backtick inside a comment in its template literal, ' +
        'which closes the literal early.',
    )
  }
}
