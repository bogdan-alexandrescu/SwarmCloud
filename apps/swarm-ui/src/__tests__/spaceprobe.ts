// THE SPACING PROBE: geometry findings read off a document this app actually
// rendered, through the stylesheet it actually ships.
//
// WHY THIS EXISTS. The owner's report was "we should look into spacing between
// sections or between vertical columns, elements should not touch dividers
// etc." That is a class of defect, not a bug, and it already produced one
// shipped regression: the density pass tightened vertical rhythm across the
// app and the capacity row let its NAME column collapse to zero while the BAR
// kept a 90px floor, so labels rendered "claude-code · 2…". Eyeballing found
// that one after it shipped. Eyeballing will not find the rest.
//
// SO THIS MEASURES RATHER THAN LOOKS, AND IT DOES NOT GREP. A verifier on an
// earlier wave neutered a guard in this app with `false &&` and the suite
// stayed green because the test only asserted that a string appeared in the
// SOURCE. Every finding below is computed from `getComputedStyle()` on an
// element `render(<App />)` produced, with the SHIPPED `styles.css` in the
// document. Deleting a rule changes the numbers here; commenting it out
// changes the numbers here; writing a rule that never matches anything
// produces no finding at all, because nothing is examined that was not
// rendered.
//
// ----------------------------------------------------------------------------
// WHAT jsdom CAN AND CANNOT DO, MEASURED RATHER THAN ASSUMED
// ----------------------------------------------------------------------------
// Checked empirically before this file was written, because two of the four
// answers are surprising:
//
//   getBoundingClientRect()   ALL ZEROS. jsdom has no layout engine. There is
//                             no box to compare with a sibling's box, so this
//                             probe cannot be a box-intersection test and does
//                             not pretend to be one. It measures the CSS that
//                             DECIDES the boxes: the padding that holds text
//                             off a border, the gap that holds two surfaces
//                             apart, the track floor that stops a column
//                             collapsing.
//   padding: var(--ctl-s3)    `paddingTop` reads "". A shorthand carrying a
//                             custom property is not expanded into longhands
//                             at all, so EVERY measurement would silently come
//                             back empty -- which reads as zero, which reads
//                             as a finding on every element in the app.
//   --a: var(--b)             `getPropertyValue('--a')` answers `var(--b)`
//                             literally. Custom properties are stored, not
//                             resolved.
//   calc(12px + 4px)          answers `calc(16px)` -- computed, then wrapped.
//
// The middle two are why `resolveSheet()` below substitutes every `var()` in
// the stylesheet TEXT before it is injected. After that the sheet contains
// only literal values, jsdom expands the shorthands normally, and the cascade,
// the specificity and the inheritance are still jsdom's work rather than this
// file's. The substitution is mechanical and is itself under test
// (`spacing.test.tsx` asserts the resolver against the real token block).
//
// A VALUE THIS FILE CANNOT MEASURE IS NEVER SKIPPED. Anything that does not
// resolve to a number -- a unit this file has no px for, a gradient where a
// colour was expected -- is returned in `unresolved` and the suite asserts
// that list against a named allowlist. A silent skip is indistinguishable from
// a pass, which is the failure mode this repository keeps producing.

// ---------------------------------------------------------------------------
// Custom properties: parse, then resolve
// ---------------------------------------------------------------------------

/** The body of every `{ ... }` whose selector matches, brace-balanced. */
function blocks(css: string, selector: RegExp): string[] {
  const out: string[] = []
  const re = new RegExp(selector.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(css)) !== null) {
    const open = css.indexOf('{', m.index + m[0].length - 1)
    if (open === -1) continue
    let depth = 0
    for (let i = open; i < css.length; i++) {
      const c = css[i]
      if (c === '{') depth++
      else if (c === '}') {
        depth--
        if (depth === 0) {
          out.push(css.slice(open + 1, i))
          break
        }
      }
    }
  }
  return out
}

/** `--name: value;` pairs in a block body, ignoring nested blocks' braces. */
function customProps(body: string): Map<string, string> {
  const out = new Map<string, string>()
  const re = /(--[A-Za-z0-9_-]+)\s*:\s*([^;}]+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(body)) !== null) out.set(m[1]!, m[2]!.trim())
  return out
}

/**
 * The two token tables the sheet declares.
 *
 * DARK is every top-level `:root { }` -- this sheet has four of them, added by
 * different waves, and the later ones deliberately only ADD names.
 * LIGHT is that, overridden by `:root:not([data-theme='dark'])`, which lives
 * inside `@media (prefers-color-scheme: light)`.
 */
export function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

export function tokenTables(source: string): { dark: Map<string, string>; light: Map<string, string> } {
  // Comments first: this sheet documents its own tokens in prose -- "`--ctl-s4:
  // 18px` was 1.5x `--ctl-s3`" -- and a declaration parser that reads prose
  // would take a deleted token's obituary for its definition.
  const css = stripComments(source)
  const dark = new Map<string, string>()
  for (const b of blocks(css, /:root\s*(?=\{)/)) for (const [k, v] of customProps(b)) dark.set(k, v)

  const light = new Map(dark)
  for (const b of blocks(css, /:root:not\(\[data-theme='dark'\]\)\s*(?=\{)/))
    for (const [k, v] of customProps(b)) light.set(k, v)

  return { dark, light }
}

/** The index of the `)` that closes the `(` at `open`. */
function closeParen(s: string, open: number): number {
  let depth = 0
  for (let i = open; i < s.length; i++) {
    if (s[i] === '(') depth++
    else if (s[i] === ')') {
      depth--
      if (depth === 0) return i
    }
  }
  return -1
}

/**
 * Replace every `var(--x)` / `var(--x, fallback)` with its value, recursively.
 *
 * An UNDECLARED name with no fallback throws rather than resolving to the
 * empty string: `padding: ` is a dropped declaration, and a dropped
 * declaration is zero padding, which this probe would then report as a real
 * finding in the wrong place. A typo has to be loud.
 */
export function resolveVars(value: string, table: ReadonlyMap<string, string>, depth = 0): string {
  if (depth > 12) throw new Error(`var() nested more than 12 deep resolving ${JSON.stringify(value)}`)
  let out = value
  for (;;) {
    const at = out.indexOf('var(')
    if (at === -1) return out
    const end = closeParen(out, at + 3)
    if (end === -1) throw new Error(`unbalanced var() in ${JSON.stringify(out)}`)
    const inner = out.slice(at + 4, end)
    const comma = inner.indexOf(',')
    const name = (comma === -1 ? inner : inner.slice(0, comma)).trim()
    const fallback = comma === -1 ? null : inner.slice(comma + 1).trim()
    const declared = table.get(name)
    if (declared === undefined && fallback === null)
      throw new Error(`${name} is used and never declared`)
    const replacement = resolveVars(declared ?? fallback!, table, depth + 1)
    out = out.slice(0, at) + replacement + out.slice(end + 1)
  }
}

/**
 * The shipped sheet with every `var()` substituted, for one theme.
 *
 * LIGHT ALSO UNWRAPS ITS MEDIA BLOCK. jsdom evaluates no media query, so
 * `@media (prefers-color-scheme: light)` never applies and a light-theme
 * measurement would silently be a dark-theme one. Promoting that one block's
 * body to the top level is what makes the light run real. Every OTHER media
 * block stays wrapped and therefore stays inert, which is correct: this probe
 * measures the layout the app serves at desktop width, and says so.
 */
export function resolveSheet(css: string, theme: 'dark' | 'light'): string {
  // COMMENTS GO FIRST. This sheet's house style is long explanatory comments,
  // and several of them quote the very syntax being substituted -- the ink
  // block opens with "Fourteen rules in this file pair `color: var(--X)` with
  // a background mixed from var(--X)". Resolving prose would make the resolver
  // throw on a token that does not exist and never did.
  let text = stripComments(css)
  if (theme === 'light') {
    const at = text.indexOf('@media (prefers-color-scheme: light)')
    if (at === -1) throw new Error('the light-theme media block is gone')
    const open = text.indexOf('{', at)
    const end = closeParen2(text, open)
    text = text.slice(0, at) + text.slice(open + 1, end) + text.slice(end + 1)
  }
  const tables = tokenTables(css)
  return resolveVars(text, theme === 'light' ? tables.light : tables.dark)
}

/** The index of the `}` closing the `{` at `open`. */
function closeParen2(s: string, open: number): number {
  let depth = 0
  for (let i = open; i < s.length; i++) {
    if (s[i] === '{') depth++
    else if (s[i] === '}') {
      depth--
      if (depth === 0) return i
    }
  }
  return -1
}

// ---------------------------------------------------------------------------
// Lengths
// ---------------------------------------------------------------------------

/** Root font size, for `rem`. `body` is 15px; `html` is the browser default. */
const ROOT_PX = 16

export interface Unresolved {
  what: string
  value: string
}

/**
 * A CSS length in px, or null when this file has no honest px for it.
 *
 * "" IS ZERO, and only after `resolveSheet`. jsdom answers "" both for a
 * property nothing declared (used value 0 for padding, margin and gap) and for
 * one whose shorthand it could not expand -- and the second case is exactly
 * what `resolveSheet` removes. Calling "" zero before that substitution would
 * make every element in the app look like it had no padding.
 */
export function px(value: string, sink?: Unresolved[], what = ''): number | null {
  const v = value.trim()
  // `initial` is what jsdom answers for a property on a control it draws
  // itself. For every length this file reads -- padding, margin, gap -- the
  // initial value IS zero, and for border-width it is `medium`, which paints
  // nothing unless a border-style is set; `borderPx` checks that first. So
  // zero is the honest answer here rather than a skip.
  if (v === 'initial') return 0
  if (v === '' || v === '0' || v === 'auto' || v === 'normal') return v === 'auto' ? null : 0
  const calc = /^calc\((.*)\)$/.exec(v)
  if (calc) return px(calc[1]!, sink, what)
  const m = /^(-?[\d.]+)(px|rem|em|ch|pt)?$/.exec(v)
  if (m) {
    const n = Number(m[1])
    switch (m[2]) {
      case undefined:
        return n === 0 ? 0 : null
      case 'px':
        return n
      case 'pt':
        return (n * 96) / 72
      case 'rem':
        return n * ROOT_PX
      // `em` and `ch` depend on the element's own font, which jsdom does not
      // resolve to a metric. Both are reported rather than guessed at.
      default:
        break
    }
  }
  if (sink) sink.push({ what, value: v })
  return null
}

// ---------------------------------------------------------------------------
// Colour, for the divider floor
// ---------------------------------------------------------------------------

export type RGBA = { r: number; g: number; b: number; a: number }

function hex(h: string): RGBA | null {
  let s = h.slice(1)
  if (s.length === 3) s = s.split('').map((c) => c + c).join('')
  if (!/^[0-9a-fA-F]{6}$/.test(s)) return null
  return { r: parseInt(s.slice(0, 2), 16), g: parseInt(s.slice(2, 4), 16), b: parseInt(s.slice(4, 6), 16), a: 1 }
}

/** Parse the colour syntaxes this sheet produces after resolution. */
export function colour(value: string, sink?: Unresolved[], what = ''): RGBA | null {
  const v = value.trim()
  if (v === '' || v === 'transparent') return { r: 0, g: 0, b: 0, a: 0 }
  if (v.startsWith('#')) return hex(v)

  const rgb = /^rgba?\(([^)]+)\)$/.exec(v)
  if (rgb) {
    const parts = rgb[1]!.split(/[,\s/]+/).filter((p) => p !== '')
    const n = parts.slice(0, 3).map(Number)
    if (n.length === 3 && n.every((x) => Number.isFinite(x))) {
      const a = parts[3] === undefined ? 1 : Number(parts[3].endsWith('%') ? Number(parts[3].slice(0, -1)) / 100 : parts[3])
      return { r: n[0]!, g: n[1]!, b: n[2]!, a: Number.isFinite(a) ? a : 1 }
    }
  }

  // `color-mix(in srgb, A P%, B)` -- the only form this sheet uses, and it uses
  // it 40-odd times for tinted borders and surfaces. Implemented rather than
  // skipped: the tinted borders are exactly the dividers this probe is for.
  const mix = /^color-mix\(\s*in\s+srgb\s*,(.*)\)$/s.exec(v)
  if (mix) {
    const [a, b] = splitTop(mix[1]!)
    if (a !== undefined && b !== undefined) {
      const pa = /([\d.]+)%\s*$/.exec(a)
      const ca = colour(pa ? a.slice(0, pa.index).trim() : a.trim(), sink, what)
      const pb = /([\d.]+)%\s*$/.exec(b)
      const cb = colour(pb ? b.slice(0, pb.index).trim() : b.trim(), sink, what)
      if (ca && cb) {
        let wa = pa ? Number(pa[1]) / 100 : pb ? 1 - Number(pb[1]) / 100 : 0.5
        wa = Math.min(1, Math.max(0, wa))
        // Non-premultiplied is wrong for partially transparent operands; every
        // use in this sheet mixes with `transparent`, where premultiplying by
        // alpha is the spec's behaviour and the visible one.
        const wb = 1 - wa
        return {
          r: ca.r * wa + cb.r * wb,
          g: ca.g * wa + cb.g * wb,
          b: ca.b * wa + cb.b * wb,
          a: ca.a * wa + cb.a * wb,
        }
      }
    }
  }

  if (sink) sink.push({ what, value: v })
  return null
}

/** Split on the top-level comma, ignoring commas inside nested functions. */
function splitTop(s: string): [string | undefined, string | undefined] {
  let depth = 0
  for (let i = 0; i < s.length; i++) {
    if (s[i] === '(') depth++
    else if (s[i] === ')') depth--
    else if (s[i] === ',' && depth === 0) return [s.slice(0, i), s.slice(i + 1)]
  }
  return [undefined, undefined]
}

/** `fg` composited over `bg`. */
export function over(fg: RGBA, bg: RGBA): RGBA {
  const a = fg.a + bg.a * (1 - fg.a)
  if (a === 0) return { r: 0, g: 0, b: 0, a: 0 }
  return {
    r: (fg.r * fg.a + bg.r * bg.a * (1 - fg.a)) / a,
    g: (fg.g * fg.a + bg.g * bg.a * (1 - fg.a)) / a,
    b: (fg.b * fg.a + bg.b * bg.a * (1 - fg.a)) / a,
    a,
  }
}

function channel(v: number): number {
  const c = Math.min(255, Math.max(0, v)) / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

/** WCAG 2.x relative luminance. */
export function luminance(c: RGBA): number {
  return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b)
}

/** WCAG 2.x contrast ratio, rounded to two places. */
export function contrast(a: RGBA, b: RGBA): number {
  const la = luminance(a)
  const lb = luminance(b)
  const hi = Math.max(la, lb)
  const lo = Math.min(la, lb)
  return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100
}

// ---------------------------------------------------------------------------
// The thresholds, and why each is the number it is
// ---------------------------------------------------------------------------

export const FLOORS = {
  /**
   * INK may not come closer than this to a border it sits inside.
   *
   * 4px, and it is `--ctl-s1` -- the smallest step this sheet's own rhythm
   * declares. Below one step there is no rhythm left to be in, and at 1x a
   * 3px gap between a glyph and a rule is something you have to look for
   * rather than something you see.
   *
   * THE FIRST VERSION OF THIS FILE USED 6px AND MEASURED THE LINE BOX, and
   * that combination was wrong in a way worth recording, because the wrongness
   * pointed at the physics rather than at the number. Every chip and tag in
   * the console came back as a finding -- and the only way to "fix" a 10px
   * chip so that its LINE BOX clears a border by 6px is to make the chip 24px
   * tall, which inflates the 30px data row that D2 settled on and that the
   * density pass exists to protect. Measuring ink instead (`inkGap`) says what
   * a reader actually sees: that same chip carries 5.1px, and passes. What is
   * left failing after the change is the set of places where the inset really
   * is a rounding error.
   */
  textInset: 4,
  /**
   * Two surfaces side by side must be held apart by at least this.
   *
   * 4px is `--ctl-s1`, the smallest step in the sheet's own rhythm. This is a
   * floor, not a target: a panel row uses s2 and a region break uses s3. What
   * it forbids is ZERO -- two cards, chips or tracks whose edges meet, which
   * is what reads as one wider element rather than two.
   */
  boxGutter: 4,
  /**
   * A divider must be this far from the surface it divides.
   *
   * 3:1 is WCAG 2.1 SC 1.4.11 Non-text Contrast, which covers "visual
   * information required to identify user interface components". A panel
   * edge, a table header rule, the rail's boundary and an input's outline are
   * all that. It is deliberately applied to EVERY rendered border here rather
   * than to a hand-picked subset: deciding case by case which border is
   * "decorative" is how a boundary that people actually navigate by gets
   * graded down to invisible, and the previous sheet already carried a comment
   * asserting "--line now at 3:1 against the surface" that measured 1.18:1.
   */
  dividerContrast: 3,
  /**
   * A REPEATED INTERIOR SEPARATOR -- the rule between two rows of the same
   * table, two stacked panels, two utilisation lines -- is held to 1.5:1
   * instead.
   *
   * WHY IT IS A DIFFERENT NUMBER AND NOT AN EXEMPTION. SC 1.4.11 is about
   * "visual information required to identify user interface components and
   * states". The edge of a control, a panel or a region is that; the
   * twenty-third rule down a list of rows is not -- nothing is identified by
   * it that the rows themselves do not already identify, and drawing all of
   * them at 3:1 turns a table of numbers into a spreadsheet grid, which is
   * exactly the register this console is trying not to be in.
   *
   * 1.5:1 is not invented here. It is the same floor
   * `tests/unit/control_plane/test_state_colour_discriminability.py` holds the
   * state triad to for greyscale separation, and it is the point at which two
   * tones stop being the same tone. The separators shipped at 1.18-1.40:1,
   * which fails even that -- so this is a real floor rather than a way of
   * excusing them.
   *
   * WHICH BORDERS GET IT IS DECIDED STRUCTURALLY, NOT BY A NAME LIST: a border
   * on exactly one side of an element that has a sibling of the same shape is
   * a separator between repeats. A name list would have let the next
   * separator-shaped thing be graded down by whoever wrote it.
   */
  separatorContrast: 1.5,
  /**
   * A text column that is allowed to truncate must still hold this much width
   * when a SIBLING track in the same grid is protected by a px floor.
   *
   * 60px is the sibling floor that makes a grid interesting to this probe, not
   * a floor on the text column itself -- see `column-can-collapse` below.
   */
  protectedSibling: 60,
} as const

/**
 * THE CORNER SCALE, and why a probe enforces it.
 *
 * The owner asked for "slightly futuristic", naming Lens, Nomad, Run:ai, the
 * OpenShift console and Rancher, and then: "I like the Logo, that should stay
 * and we should take it into account when designing the look and feel
 * overall." What those five consoles have in common, and what SwarmMark has,
 * is that nothing in them is approximate -- the mark is six discs at exact
 * 60-degree spacing around a centre, with two stroke weights and no third.
 *
 * A sheet with radii at 2, 4, 6, 8, 10 and 14px is approximate. Nobody can see
 * the difference between a 6px and an 8px corner side by side, so the extra
 * value buys nothing and costs the one property the reference products have:
 * that the surfaces look measured. This is the scale; a corner not on it is a
 * finding.
 *
 *   2px   the track, and anything the width of a track
 *   6px   --ctl-radius-sm: chips, cells, small controls
 *   10px  --radius: panels and cards
 *   14px  --ctl-radius-lg: the large containers
 *   999px a pill, which is a shape rather than a radius
 */
export const RADII = [0, 2, 6, 10, 14, 999] as const

// ---------------------------------------------------------------------------
// The findings
// ---------------------------------------------------------------------------

export type Kind =
  | 'text-abuts-divider'
  | 'surfaces-touch'
  | 'divider-under-floor'
  | 'column-can-collapse'
  | 'radius-off-scale'

export interface Finding {
  kind: Kind
  /** tag + classes, which is the thing a fix is written against. */
  where: string
  side: string
  measured: number
  floor: number
  detail: string
}

export interface Report {
  findings: Finding[]
  unresolved: Unresolved[]
  /** Boxes whose sideways inset comes from centring, which this cannot measure. */
  centred: string[]
  /** How much was actually looked at, so a probe that stops seeing says so. */
  elements: number
  /**
   * The elements that actually draw a border.
   *
   * Returned so the SECOND theme can be measured without cascading the whole
   * document again. Only one rule here depends on the theme -- the contrast of
   * a divider against its ground -- and only a bordered element can produce
   * one. Whether a border EXISTS is theme-independent in this sheet: the light
   * block redefines colour tokens and nothing else, so no rule gains or loses
   * a border-width or a border-style when the theme flips.
   */
  bordered: Element[]
}

const SIDES = ['Top', 'Right', 'Bottom', 'Left'] as const

function signature(el: Element): string {
  const cls = [...el.classList].sort().join('.')
  return cls === '' ? el.tagName.toLowerCase() : `${el.tagName.toLowerCase()}.${cls}`
}

/**
 * `getComputedStyle` is the expensive call here -- jsdom re-runs the cascade
 * for every one of them -- and this file asks for the same element's style
 * from six different rules. Cached for the duration of one `probe()`, and
 * dropped afterwards so a second sweep never reads the first sweep's sheet.
 */
let STYLE_CACHE = new WeakMap<Element, CSSStyleDeclaration>()

function styleOf(el: Element): CSSStyleDeclaration {
  const hit = STYLE_CACHE.get(el)
  if (hit !== undefined) return hit
  const s = getComputedStyle(el)
  STYLE_CACHE.set(el, s)
  return s
}

function len(s: CSSStyleDeclaration, prop: string, sink: Unresolved[], where: string): number {
  const v = px(s.getPropertyValue(prop), sink, `${where} ${prop}`)
  return v ?? 0
}

/** A border edge that is actually drawn. */
function borderPx(s: CSSStyleDeclaration, side: string): number {
  const style = s.getPropertyValue(`border-${side.toLowerCase()}-style`)
  if (style === '' || style === 'none' || style === 'hidden') return 0
  return px(s.getPropertyValue(`border-${side.toLowerCase()}-width`)) ?? 0
}

function hasDirectText(el: Element): boolean {
  for (const n of el.childNodes) {
    if (n.nodeType === 3 && (n.textContent ?? '').trim() !== '') return true
  }
  return false
}

/**
 * The used font size in px, and the used line height, INHERITED BY HAND.
 *
 * jsdom does not inherit `font-size` or `line-height` to an element no rule
 * targets: `getComputedStyle(<span class="brand-env-name">).fontSize` is the
 * empty string even though its parent declares `600 10.5px/1.7`. That is a
 * real gap and it was silently making this probe far stricter than the
 * product: an empty string parses as zero, a zero font has a zero line box,
 * and a zero line box has no leading -- so every element the sheet does not
 * name individually looked like text pressed flat against its container's
 * border. Two of the findings this file reported were that, not the product.
 *
 * So the value is taken from the nearest ancestor that states one, which is
 * what inheritance means. 15px is `body`'s declared size and the last resort.
 */
function inherited(el: Element | null, prop: 'fontSize' | 'lineHeight'): string {
  for (let a = el; a !== null; a = a.parentElement) {
    const v = styleOf(a)[prop].trim()
    if (v !== '') return v
  }
  return prop === 'fontSize' ? '15px' : 'normal'
}

function fontPxOf(el: Element): number {
  const v = px(inherited(el, 'fontSize'))
  return v === null || v <= 0 ? 15 : v
}

/**
 * The height of one line box.
 *
 * `normal` is 1.2 here. The real figure is the font's own ascent + descent +
 * line gap, which no engine will tell you without a font; 1.2 is what every
 * browser lands within a few percent of for a UI sans and a UI mono, and it is
 * used ONLY to compute how much air a line already has, so being a little low
 * makes this probe stricter rather than blinder.
 */
function lineBox(el: Element): number {
  const f = fontPxOf(el)
  const lh = inherited(el, 'lineHeight')
  if (lh === 'normal') return f * 1.2
  const n = Number(lh)
  if (Number.isFinite(n)) return n * f
  return px(lh) ?? f * 1.2
}

/**
 * Cap height as a fraction of the font size.
 *
 * 0.72 for the two families this sheet uses (a UI sans and a UI mono, both
 * around .70-.73). This matters because the quantity the owner's complaint is
 * about is the gap between INK and a rule, not between a line box and a rule.
 * A chip set `10.5px/1.7` has a 17.9px line box around 7.6px of ink, so it
 * already carries 5.1px of air on each side before a single pixel of padding
 * is declared -- and measuring the line box instead would report every dense
 * chip in this console as text pressed against its own border. The "fix" for
 * that false finding would have been to inflate the rows the density pass
 * exists to tighten.
 */
const CAP = 0.72

/**
 * The air between the top of the ink and the top of the line box.
 *
 * Uppercase is assumed, which is what every chip, tag and column head in this
 * sheet sets (`text-transform: uppercase`), and which is the STRICTER
 * assumption: lowercase x-height ink is smaller, so its gap is larger.
 */
function inkGap(el: Element): number {
  return Math.max(0, (lineBox(el) - fontPxOf(el) * CAP) / 2)
}

/** Does this element centre its content on the axis that `side` lies on? */
function centres(s: CSSStyleDeclaration, side: string): boolean {
  const flexy = s.display.includes('flex')
  const griddy = s.display.includes('grid')
  const column = s.flexDirection.startsWith('column')
  const vertical = side === 'Top' || side === 'Bottom'
  if (!flexy && !griddy) return false
  const cross = vertical ? (column ? s.justifyContent : s.alignItems) : column ? s.alignItems : s.justifyContent
  return cross === 'center'
}

/** An explicit px size on the axis `side` lies on, if the sheet states one. */
function fixedSize(s: CSSStyleDeclaration, side: string): number | null {
  const vertical = side === 'Top' || side === 'Bottom'
  const a = px(vertical ? s.height : s.width)
  if (a !== null && a > 0) return a
  const b = px(vertical ? s.minHeight : s.minWidth)
  return b !== null && b > 0 ? b : null
}

function rendered(el: Element): boolean {
  const s = styleOf(el)
  return s.display !== 'none' && s.visibility !== 'hidden'
}

/** Element children that are laid out in flow. */
function boxChildren(el: Element): Element[] {
  return [...el.children].filter((c) => {
    const s = styleOf(c)
    return s.display !== 'none' && s.position !== 'absolute' && s.position !== 'fixed'
  })
}

/**
 * A PANEL: a child that paints a surface a reader can see AND carries words.
 *
 * Both halves matter, and the first run of this probe proved why.
 *
 *   WITHOUT "can see", the rail's section buttons looked like four surfaces
 *   touching at a 1px gutter. They are `background: none` with a TRANSPARENT
 *   2px left border reserved for the selection rule -- nothing is painted
 *   until one of them is chosen, and only one ever is.
 *
 *   WITHOUT "carries words", the segments of a meter look like surfaces
 *   touching at a gutter of a pixel or two. The first run met this on the
 *   five cells of Accounts' old window bar (`.acct-bar`, collapsed into the
 *   shared track by CP-25); a track's fill beside its over-ceiling segment is
 *   the same shape. They are the parts of ONE meter, and parts of a meter are
 *   supposed to touch or nearly touch; the gutter rule is about content
 *   blocks standing next to each other.
 */
function isPanel(el: Element): boolean {
  if ((el.textContent ?? '').trim() === '') return false
  const s = styleOf(el)
  const bg = colour(s.backgroundColor)
  if (bg !== null && bg.a > 0.02) return true
  if (s.backgroundImage !== '' && s.backgroundImage !== 'none') return true
  return SIDES.some((side) => {
    if (borderPx(s, side) <= 0) return false
    const c = colour(s.getPropertyValue(`border-${side.toLowerCase()}-color`))
    return c !== null && c.a > 0.02
  })
}

/**
 * ONE CONTROL WITH SEGMENTS, NOT A ROW OF CONTENT BLOCKS.
 *
 * The third exemption to the gutter floor, and it is the same argument the
 * meter one above makes: `boxGutter` forbids ZERO between two cards,
 * chips or tracks, "which is what reads as one wider element rather than two".
 * A segmented control is the case where reading as one element is the whole
 * design intent -- `design-system.md` §6.11 settles it as "ONE BORDERED GROUP,
 * NOT N PILLS", with 1px internal dividers and the active segment marked by a
 * surface step. Its segments MUST touch; a 4px gutter between them would
 * produce the row of pills the primitive exists to replace.
 *
 * FOUND BY RENDERING IT. `.ctl-seg` shipped in the foundation pass and no
 * screen used it, so this probe never saw one. The first screen to render it
 * -- the run list's Live / Waiting / Recent tabs -- reported `3 surfaces in a
 * row with a 0px gutter` in both themes: the active segment is a surface
 * because it paints `--surface-2`, and the other two are surfaces because
 * `button + button` draws the divider as a left border on the child.
 *
 * THE PREDICATE IS STRUCTURAL, not a class name, so it cannot rot into an
 * allowlist: a container that paints its own visible border, whose every
 * surface-bearing child is a `<button>`, is one bordered group of controls.
 * A row of cards fails it on the first count (cards are not buttons); a
 * toolbar of loose buttons fails it on the second (a toolbar paints no border
 * of its own). Only a bordered group of segments passes.
 */
function isSegmentedGroup(el: Element, surfaces: readonly Element[]): boolean {
  if (surfaces.length === 0) return false
  if (!surfaces.every((k) => k.tagName === 'BUTTON')) return false
  const s = styleOf(el)
  return SIDES.some((side) => {
    if (borderPx(s, side) <= 0) return false
    const c = colour(s.getPropertyValue(`border-${side.toLowerCase()}-color`))
    return c !== null && c.a > 0.02
  })
}

/**
 * How far the nearest text is held off `side`, and how tall (or wide) the
 * content inside this element is.
 *
 * jsdom gives no boxes -- `getBoundingClientRect()` is all zeros -- so this is
 * not a pixel measured off a rendered glyph. It is the sum of the three things
 * that actually decide that pixel, walked down the chain of frames between the
 * border and the first text:
 *
 *   PADDING and MARGIN   declared, and the thing a fix changes.
 *   HALF-LEADING         the air a line box already carries.
 *   CENTRING SLACK       a 44px bar with `align-items: center` holds an 18px
 *                        line 12px off its own rule without declaring one
 *                        pixel of padding. Ignoring that reported the product
 *                        header, the page head and the dock as "text against a
 *                        divider" when all three are correct, and the fix
 *                        would have been to pad chrome that is already the
 *                        right height.
 *
 * `size` is the content extent on the same axis, which is what the slack is
 * computed against one level up.
 */
interface TextInset {
  inset: number
  size: number
}

function insetToText(
  el: Element,
  side: string,
  sink: Unresolved[],
  centredSideways: string[],
  depth = 0,
): TextInset | null {
  const s = styleOf(el)
  const lower = side.toLowerCase()
  const opposite = { Top: 'bottom', Bottom: 'top', Left: 'right', Right: 'left' }[side]!
  const padA = len(s, `padding-${lower}`, sink, signature(el))
  const padB = len(s, `padding-${opposite}`, sink, signature(el))
  const bords = borderPx(s, side) + borderPx(s, opposite.replace(/^./, (c) => c.toUpperCase()))

  let inner: TextInset | null = null
  if (hasDirectText(el)) {
    inner = { inset: inkGap(el), size: side === 'Top' || side === 'Bottom' ? lineBox(el) : 0 }
  } else if (depth < 6) {
    const kids = boxChildren(el)
    for (const kid of kids) {
      const ks = styleOf(kid)
      const mA = len(ks, `margin-${lower}`, sink, signature(kid))
      const mB = len(ks, `margin-${opposite}`, sink, signature(kid))
      const r = insetToText(kid, side, sink, centredSideways, depth + 1)
      if (r === null) continue
      const cand = { inset: mA + r.inset, size: mA + mB + r.size }
      if (inner === null || cand.inset < inner.inset) inner = cand
      else if (cand.size > inner.size) inner = { inset: inner.inset, size: cand.size }
    }
  }
  if (inner === null) return null // no text sits against this border

  const vertical = side === 'Top' || side === 'Bottom'
  let slack = 0
  const H = fixedSize(s, side)
  if (H !== null && centres(s, side)) {
    if (vertical) slack = Math.max(0, (H - bords - padA - padB - inner.size) / 2)
    else {
      // HORIZONTALLY THIS FILE DOES NOT GUESS. The slack a centred box gives
      // its text sideways is the box width minus the text's ADVANCE WIDTH, and
      // nothing here can measure an advance width without a font. Estimating
      // it would let the probe exempt a real finding on a number it made up.
      // So the element is named instead, and `spacing.test.tsx` holds that
      // list against an allowlist with a reason for each entry.
      centredSideways.push(`${signature(el)} [${lower}] width ${H}px, centred`)
      return { inset: Number.POSITIVE_INFINITY, size: H }
    }
  }

  return { inset: padA + slack + inner.inset, size: Math.max(H ?? 0, bords + padA + padB + inner.size) }
}

/** Count the tracks in a `grid-template-columns` value. */
function tracks(value: string): string[] {
  const out: string[] = []
  let depth = 0
  let cur = ''
  for (const ch of value) {
    if (ch === '(' || ch === '[') depth++
    else if (ch === ')' || ch === ']') depth--
    if (/\s/.test(ch) && depth === 0) {
      if (cur.trim() !== '') out.push(cur.trim())
      cur = ''
    } else cur += ch
  }
  if (cur.trim() !== '') out.push(cur.trim())
  return out.filter((t) => !t.startsWith('['))
}

/** The minimum width a track is guaranteed, in px, or null for "unbounded". */
function trackFloor(t: string): number | null {
  const mm = /^minmax\((.*)\)$/.exec(t)
  if (mm) {
    const [lo] = splitTop(mm[1]!)
    return lo === undefined ? null : px(lo.trim())
  }
  if (/^(auto|min-content|max-content|[\d.]+fr)$/.test(t)) return null
  if (t.startsWith('repeat(')) return null
  return px(t)
}

/**
 * Walk one rendered document and report every geometric finding in it.
 */
/**
 * Elements a geometry rule could possibly reach.
 *
 * WHY NOT JUST WALK EVERY ELEMENT. jsdom's `getComputedStyle` runs the whole
 * cascade on every call -- 3,200 rules against one element -- and the fifteen
 * rendered routes hold about 1,700 elements between them. Cascading all of
 * them twice, once per theme, put this sweep at two minutes inside a gate that
 * the Makefile explicitly keeps cheap enough that nobody skips it.
 *
 * Most of those elements cannot produce a finding: a `<span>` that no rule
 * gives a border, a radius, a gap or a grid to has no geometry to get wrong.
 * So the selectors that DO declare one are read out of the sheet the document
 * is actually using, and only what they match is measured. A rule added later
 * is picked up automatically, because this reads the sheet rather than a list.
 *
 * THE FOUR TAG NAMES ARE NOT AN AFTERTHOUGHT. A `<button>`, `<input>`,
 * `<select>` or `<textarea>` that the product never styled still draws the
 * browser's own border, and "a native control shipped into a dark console"
 * is precisely the kind of finding this probe should make -- it found one on
 * its first run. Those borders come from the UA sheet, which is not in
 * `document.styleSheets`, so they are named here.
 */
const GEOMETRY = /(^|;|\s)(border|border-[a-z]+|gap|row-gap|column-gap|display|grid-template-columns)\s*:/
const NATIVE_CONTROLS = ['button', 'input', 'select', 'textarea', 'fieldset', 'hr', 'table']

function geometryCandidates(root: ParentNode): Element[] {
  const selectors = new Set<string>(NATIVE_CONTROLS)
  for (const sheet of [...document.styleSheets]) {
    let rules: CSSRuleList
    try {
      rules = sheet.cssRules
    } catch {
      continue
    }
    const walk = (list: CSSRuleList) => {
      for (const r of list) {
        if (r.constructor.name === 'CSSMediaRule') walk((r as CSSMediaRule).cssRules)
        else if (r.constructor.name === 'CSSStyleRule') {
          const rule = r as CSSStyleRule
          if (GEOMETRY.test(rule.style.cssText)) selectors.add(rule.selectorText)
        }
      }
    }
    walk(rules)
  }

  const out = new Set<Element>()
  for (const sel of selectors) {
    for (const one of sel.split(',')) {
      // A pseudo-class or pseudo-element describes a STATE or a part, not an
      // element in this document; stripping it leaves the element the rule is
      // about, which is the one to measure. An unparseable remainder is
      // skipped rather than thrown, because a sheet may legitimately carry
      // `::-webkit-scrollbar`.
      const base = one.replace(/::?[a-zA-Z-]+(\([^)]*\))?/g, '').trim()
      if (base === '' || base === '*') continue
      try {
        for (const el of root.querySelectorAll(base)) out.add(el)
      } catch {
        continue
      }
    }
  }
  return [...out]
}

export function probe(root: ParentNode, restrict?: readonly Element[]): Report {
  STYLE_CACHE = new WeakMap()
  const unresolved: Unresolved[] = []
  const centred: string[] = []
  const bordered: Element[] = []
  const seen = new Map<string, Finding>()
  // ONE REPRESENTATIVE PER DISTINCT SHAPE. A finding is reported against a
  // signature -- `td.acct-window.n [bottom]` -- because that is what a fix is
  // written against, and the forty cells that share a signature inside one
  // table share a cascade. Measuring all forty costs forty full cascades in
  // jsdom, which is where this sweep's fifty seconds went, and adds nothing.
  //
  // THE KEY IS NOT JUST THE SIGNATURE, and the first version that thought it
  // was lost nine real findings without saying so. Two things beyond the
  // class list decide what an element is given:
  //
  //   ITS WHOLE ANCESTRY, because what is painted behind it is a term in the
  //   contrast ratio, and the background can come from any level up. Keying on
  //   the immediate parent alone collapsed a `th` in a `thead` onto a `th` in
  //   a `tbody` -- same tag, same parent shape, different ground -- and lost
  //   eight findings while reporting nothing missing.
  //   ITS POSITION, because this sheet styles by position. `.ctl-util +
  //   .ctl-util` draws the separator on the SECOND one and later, and a
  //   dedupe that kept the first `.ctl-util` kept the only one with no
  //   border. `.ctl-table tbody tr:last-child td` removes it again. So first,
  //   last, and "has a twin before it" are part of the shape. (`:nth-child`
  //   is not: `grep -n 'nth-child' src/styles.css src/*.tsx` is empty, and if
  //   one is added its striping will be a new shape under a key that does not
  //   know about it -- which is worth saying out loud here.)
  const shapes = new Set<string>()
  const all = restrict ?? geometryCandidates(root)
    .filter(rendered)
    .filter((el) => {
      const me = signature(el)
      const prev = el.previousElementSibling
      const pos =
        (prev === null ? 'F' : '') +
        (el.nextElementSibling === null ? 'L' : '') +
        (prev !== null && signature(prev) === me ? 'P' : '')
      const chain: string[] = []
      for (let a = el.parentElement; a !== null && a !== root; a = a.parentElement) chain.push(signature(a))
      const key = `${chain.join('>')}>${me}|${pos}`
      if (shapes.has(key)) return false
      shapes.add(key)
      return true
    })

  const add = (f: Finding) => {
    const key = `${f.kind}|${f.where}|${f.side}`
    const prior = seen.get(key)
    if (prior === undefined || f.measured < prior.measured) seen.set(key, f)
  }

  for (const el of all) {
    const s = styleOf(el)
    const where = signature(el)

    // A CHECKBOX IS A MARK THE PLATFORM DRAWS, NOT A SURFACE THIS PRODUCT
    // DOES. Its border, its padding and its size come from the user agent by
    // design -- that is what makes it look and behave like every other
    // checkbox the reader has ever used, and it is why `styles.css` sets
    // `accent-color` on it and nothing else. Measuring the UA's `initial`
    // border against this sheet's floors would report the one control that is
    // correctly left alone.
    const type = (el.getAttribute('type') ?? '').toLowerCase()
    if (el.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio')) continue

    const separator = isRepeatSeparator(el)
    if (SIDES.some((side) => borderPx(s, side) > 0)) bordered.push(el)

    // ---- 1. a border, and what is behind it -----------------------------
    for (const Side of SIDES) {
      const w = borderPx(s, Side)
      if (w <= 0) continue

      const c = colour(s.getPropertyValue(`border-${Side.toLowerCase()}-color`), unresolved, `${where} border-${Side.toLowerCase()}-color`)
      const bg = behind(el, unresolved)
      if (c !== null && bg !== null && c.a > 0.05) {
        const ratio = contrast(over(c, bg), bg)
        const floor = separator ? FLOORS.separatorContrast : FLOORS.dividerContrast
        if (ratio < floor) {
          add({
            kind: 'divider-under-floor',
            where,
            side: Side.toLowerCase(),
            measured: ratio,
            floor,
            detail: `${separator ? 'separator' : 'boundary'} ${rgbText(c)} on ${rgbText(bg)}`,
          })
        }
      }

      // ---- 2. text against that border ----------------------------------
      // A TRANSPARENT BORDER IS NOT A DIVIDER. This sheet reserves several --
      // `.ctl-nav-link` keeps a 2px transparent left border so that selecting
      // it moves no text, and `.ov-link` keeps a 1px transparent bottom one so
      // that hovering does not. Nothing is drawn, so nothing can be abutted,
      // and counting them reported two rules as defects for doing the right
      // thing.
      if (c === null || c.a <= 0.05) continue
      const r = insetToText(el, Side, unresolved, centred)
      if (r !== null && Number.isFinite(r.inset) && r.inset < FLOORS.textInset) {
        const inset = Math.round(r.inset * 100) / 100
        add({
          kind: 'text-abuts-divider',
          where,
          side: Side.toLowerCase(),
          measured: inset,
          floor: FLOORS.textInset,
          detail: `text sits ${inset}px from a ${w}px border`,
        })
      }
    }

    // ---- 2b. a corner that is not on the scale --------------------------
    // The longhands FIRST, the shorthand as a fallback: jsdom expands
    // `padding` into its longhands but not `border-radius`, so reading only
    // `borderTopLeftRadius` finds nothing on the many rules that write the
    // shorthand -- which is every rounded thing in this sheet.
    const shorthand = s.getPropertyValue('border-radius').trim()
    // A MARK IS NOT A BOX. The chip shapes -- the 6px diamond that means
    // failed, the 8x3 bar that means "a fact rather than a verdict", the 5x10
    // cells of the account meter -- are drawn at glyph scale, where a 1px
    // corner is part of the shape and the 6px surface radius would round them
    // into blobs. The scale governs surfaces; anything that declares itself
    // 12px or smaller on both axes is not one. Measured from the declaration,
    // so nothing is exempted by being named.
    const w = px(s.width)
    const h = px(s.height)
    const isMark = w !== null && h !== null && w > 0 && h > 0 && w <= 12 && h <= 12
    for (const corner of isMark ? [] : (['top-left', 'top-right', 'bottom-right', 'bottom-left'] as const)) {
      const long = s.getPropertyValue(`border-${corner}-radius`).trim()
      const raw = long !== '' ? long : shorthand
      if (raw === '') continue
      if (raw.includes('/')) {
        unresolved.push({ what: `${where} border-radius`, value: raw })
        break
      }
      // A two-value radius is an ellipse; this sheet declares none, and one
      // appearing is worth a look rather than a silent pass.
      const parts = raw.split(/\s+/)
      for (const p of parts) {
        if (p.endsWith('%')) continue // a percentage corner is a shape, like a pill
        const r = px(p, unresolved, `${where} border-${corner}-radius`)
        if (r === null) continue
        if (!RADII.includes(Math.round(r) as (typeof RADII)[number])) {
          add({
            kind: 'radius-off-scale',
            where,
            side: corner,
            measured: r,
            floor: 0,
            detail: `${r}px is not one of ${RADII.join(', ')}`,
          })
        }
      }
    }

    // ---- 3. two surfaces with no gutter ---------------------------------
    const display = s.display
    if (display === 'flex' || display === 'inline-flex' || display === 'grid' || display === 'inline-grid') {
      const kids = boxChildren(el)
      const surfaces = kids.filter(isPanel)
      if (surfaces.length >= 2 && !isSegmentedGroup(el, surfaces)) {
        const cols = display.endsWith('grid') ? tracks(s.gridTemplateColumns || '') : []
        const multiCol =
          display.endsWith('grid') ? cols.length >= 2 || /repeat\(/.test(s.gridTemplateColumns || '') : !s.flexDirection.startsWith('column')
        const multiRow = display.endsWith('grid')
          ? kids.length > Math.max(1, cols.length)
          : s.flexDirection.startsWith('column') || s.flexWrap === 'wrap'

        const colGap = gapOf(s, 'column', unresolved, where)
        const rowGap = gapOf(s, 'row', unresolved, where)

        if (multiCol && colGap !== null && colGap < FLOORS.boxGutter && !marginsCover(surfaces, 'Left', 'Right', unresolved)) {
          add({
            kind: 'surfaces-touch',
            where,
            side: 'column',
            measured: colGap,
            floor: FLOORS.boxGutter,
            detail: `${surfaces.length} surfaces in a row with a ${colGap}px gutter`,
          })
        }
        if (multiRow && rowGap !== null && rowGap < FLOORS.boxGutter && !marginsCover(surfaces, 'Top', 'Bottom', unresolved)) {
          add({
            kind: 'surfaces-touch',
            where,
            side: 'row',
            measured: rowGap,
            floor: FLOORS.boxGutter,
            detail: `${surfaces.length} stacked surfaces with a ${rowGap}px gutter`,
          })
        }
      }
    }

    // ---- 4. a label column that can collapse while a picture cannot -----
    //
    // THE SHAPE OF THE ONE THAT SHIPPED, stated precisely so this does not
    // fire on every `minmax(0, 1fr)` in the app -- most of which are correct,
    // because a long task id SHOULD give way. What went wrong in the capacity
    // row was narrower than that and is worth naming exactly:
    //
    //   a LABEL track was allowed to reach zero, while a track holding a
    //   PICTURE -- a bar, a meter, something with no words in it -- kept a
    //   px floor. So under pressure the product dropped the only part of the
    //   row a reader cannot reconstruct, and kept the part they can see at a
    //   glance anyway. "claude-code · 2…" next to a perfectly intact bar.
    //
    // Three conditions, all required: one child per track (so a child can be
    // attributed to a track at all), the collapsible track's own child
    // truncates its text, and some OTHER track is both protected and wordless.
    if (display.endsWith('grid')) {
      const cols = tracks(s.gridTemplateColumns || '')
      const kids = boxChildren(el)
      if (cols.length >= 2 && kids.length === cols.length) {
        const floors = cols.map(trackFloor)
        const wordlessProtected = kids.some(
          (k, j) => (floors[j] ?? 0) >= FLOORS.protectedSibling && (k.textContent ?? '').trim() === '',
        )
        if (wordlessProtected) {
          cols.forEach((t, i) => {
            const kid = kids[i]
            if (kid === undefined) return
            const collapsible = /^minmax\(\s*0/.test(t) || floors[i] === 0
            const truncates =
              styleOf(kid).textOverflow === 'ellipsis' ||
              [...kid.querySelectorAll('*')].some((d) => styleOf(d).textOverflow === 'ellipsis')
            if (collapsible && truncates) {
              add({
                kind: 'column-can-collapse',
                where,
                side: `track ${i + 1}`,
                measured: 0,
                floor: FLOORS.protectedSibling,
                detail: `${t} holds truncating text beside a wordless ${Math.max(...floors.map((x) => x ?? 0))}px floor`,
              })
            }
          })
        }
      }
    }
  }

  return {
    findings: [...seen.values()].sort(
      (a, b) => a.kind.localeCompare(b.kind) || a.where.localeCompare(b.where) || a.side.localeCompare(b.side),
    ),
    unresolved,
    centred: [...new Set(centred)].sort(),
    elements: all.length,
    bordered,
  }
}

function rgbText(c: RGBA): string {
  const n = (x: number) => Math.round(x)
  return c.a >= 1 ? `rgb(${n(c.r)} ${n(c.g)} ${n(c.b)})` : `rgba(${n(c.r)} ${n(c.g)} ${n(c.b)} / ${c.a.toFixed(2)})`
}

/** `gap` is a shorthand jsdom does not expand either; read both spellings. */
function gapOf(s: CSSStyleDeclaration, axis: 'row' | 'column', sink: Unresolved[], where: string): number | null {
  const own = s.getPropertyValue(`${axis}-gap`)
  if (own.trim() !== '') return px(own, sink, `${where} ${axis}-gap`)
  const short = s.getPropertyValue('gap').trim()
  if (short === '') return 0
  const parts = short.split(/\s+/)
  const want = axis === 'row' ? parts[0]! : (parts[1] ?? parts[0]!)
  return px(want, sink, `${where} gap`)
}

/** Do the children hold themselves apart with margins instead of a gap? */
function marginsCover(kids: Element[], a: string, b: string, sink: Unresolved[]): boolean {
  return kids.every((k) => {
    const s = styleOf(k)
    const ma = len(s, `margin-${a.toLowerCase()}`, sink, signature(k))
    const mb = len(s, `margin-${b.toLowerCase()}`, sink, signature(k))
    return ma + mb >= FLOORS.boxGutter
  })
}

/**
 * Is this border the rule BETWEEN two repeats of the same thing?
 *
 * Two conditions, both structural, so nothing is graded down by being named:
 * the border is on exactly one side (a rule, not a frame), and the element has
 * a sibling built the same way (a row among rows, a panel among panels).
 *
 * The FIRST cell of the FIRST row of a table has no preceding sibling, which
 * is why the NEXT one counts too -- otherwise the top-left cell of every table
 * in the product would be the one boundary held to a different floor from the
 * twenty identical cells beside it.
 */
function isRepeatSeparator(el: Element): boolean {
  const s = styleOf(el)
  const drawn = SIDES.filter((side) => borderPx(s, side) > 0)
  if (drawn.length !== 1) return false
  const side = drawn[0]!
  const me = signature(el)
  const colour = s.getPropertyValue(`border-${side.toLowerCase()}-color`)

  // A TABLE CELL'S BOTTOM RULE IS THE TABLE'S ROW RULE, always. There is only
  // one kind of it in a given table, and whether a particular row happens to
  // hold eight cells or one -- an empty-state row with a `colSpan`, which is
  // what this last case turned out to be -- does not change what the rule is
  // or what it separates.
  if ((el.tagName === 'TD' || el.tagName === 'TH') && side === 'Bottom') return true

  const twin = (other: Element | null): boolean => {
    if (other === null) return false
    // Same class list is the easy case -- `.ctl-util + .ctl-util`, one section
    // after another.
    if (signature(other) === me) return true
    // The harder one, and the one the first version of this got wrong: the
    // cells of a table row are the same kind of thing and do NOT share a class
    // list. `th.pool-name` sits beside `th.mono`, `td.n` beside
    // `td.acct-window.n`, and grading each of those as a component boundary
    // demanded 3:1 of every rule in every table -- which is the spreadsheet
    // grid this whole distinction exists to avoid. Same tag, same one rule,
    // same colour: a repeat.
    // A table's row header and its cells are the same kind of thing. The
    // dense tables here write `<th scope="row">` for the name and `<td>` for
    // the figures, so a row header's only siblings are `td`s -- and requiring
    // an identical tag graded the first column of every table as a component
    // boundary while the rest of the row was a separator.
    const cell = (x: Element) => x.tagName === 'TD' || x.tagName === 'TH'
    if (other.tagName !== el.tagName && !(cell(other) && cell(el))) return false
    const os = styleOf(other)
    const odrawn = SIDES.filter((x) => borderPx(os, x) > 0)
    return (
      odrawn.length === 1 &&
      odrawn[0] === side &&
      os.getPropertyValue(`border-${side.toLowerCase()}-color`) === colour
    )
  }

  // Either neighbour counts. The FIRST cell of the FIRST row of a table has no
  // preceding sibling, and grading only that one differently from the twenty
  // identical cells beside it would be an accident of position.
  return twin(el.previousElementSibling) || twin(el.nextElementSibling)
}

/** The first opaque background painted behind `el`, composited on the way up. */
function behind(el: Element, sink: Unresolved[]): RGBA | null {
  let acc: RGBA = { r: 0, g: 0, b: 0, a: 0 }
  let node: Element | null = el.parentElement
  while (node !== null) {
    const s = styleOf(node)
    if (s.backgroundImage !== '' && s.backgroundImage !== 'none') {
      // A hatch or a gradient. Named, not skipped -- the same rule
      // test_ui_contrast.py applies to the two it cannot resolve.
      sink.push({ what: `${signature(node)} background-image`, value: s.backgroundImage })
      return null
    }
    const c = colour(s.backgroundColor, sink, `${signature(node)} background-color`)
    if (c === null) return null
    acc = over(acc, c)
    if (acc.a >= 0.999) return acc
    node = node.parentElement
  }
  return acc.a > 0 ? acc : null
}
