// §B4.1 -- THE TYPE SCALE, AS AN ASSERTION.
//
// WHAT THIS EXISTS TO STOP. Before this pass there were 240 raw
// `font-size: <n>px` / `font: <n>px` declarations across the stylesheet and
// four components, at seventeen different sizes between 9.5 and 24px. Nobody
// added them carelessly; each one was a reasonable local decision, and
// seventeen reasonable local decisions is what a scale exists to replace. The
// 241st would have been reasonable too. So the count is held at zero here
// rather than audited by hand again in six months.
//
// WHY THIS ONE IS A SOURCE SCAN, when the rest of this suite refuses to be.
// `shell.test.tsx` and `brand.test.tsx` are emphatic that a grep over source
// proves nothing -- a verifier on an earlier wave neutered a guard with
// `false &&` and the suite stayed green. That argument is about BEHAVIOUR: you
// cannot tell whether a guard runs by looking at it. This is the opposite kind
// of claim. "No rule in this sheet is written in raw pixels" is a property of
// the TEXT, and there is no DOM question that answers it: `getComputedStyle`
// can only be asked about elements that happen to be rendered, so a rule on a
// screen this suite never mounts would go unchecked, which is exactly where a
// stray size would survive. The scan reads every file, mounted or not.
//
// It is paired with DOM assertions below, which is what keeps it honest: the
// scan proves nothing raw is WRITTEN, the DOM assertions prove the tokens are
// actually in the cascade and resolve to the stated values, so the two
// together cannot both pass on a stylesheet that failed to load.
//
// THE jsdom TRAP, which this file is designed around rather than tripped by.
// jsdom does not resolve `var()`: `getComputedStyle(el).fontSize` answers the
// literal string `var(--t-body)`, and a `font:` SHORTHAND carrying a custom
// property is not expanded into longhands at all -- so a rule written as
// `font: 600 var(--t-meta)/var(--lh-meta) var(--mono)` contributes NOTHING to
// a computed style here. Every DOM assertion below therefore either reads a
// custom property off `:root` (which jsdom does resolve, because the value is
// a literal) or reads a longhand off a rule written as longhands. The sheet
// writes `.section > h2` as longhands for this reason and says so there.

// `?raw` rather than `node:fs`, for the reason brand.test.tsx gives: it is
// Vite's own loader and resolves the same paths the bundle would, so a moved
// or renamed file fails to resolve here instead of quietly reading a stale
// copy from disk -- and a scan whose input silently becomes empty is a scan
// that passes hardest when it is most broken.
import STYLES from '../styles.css?raw'
import AGENT_DETAIL from '../AgentDetail.tsx?raw'
import HELP_CARD from '../HelpCard.tsx?raw'
import HELP_SECTION from '../HelpSection.tsx?raw'
import OVERVIEW from '../Overview.tsx?raw'
import { describe, expect, it } from 'vitest'

/**
 * The six steps, with the line-height that is part of each one.
 *
 * WHAT MOVED IN THE RESTRAINT PASS, and why this array is the record of it.
 * The owner's verdict on the shipped console was "the design looks almost
 * cartoonish", and the measurement found the top of this ladder to be the
 * largest single cause: `--t-figure` was 30px against a 14px body -- 2.14:1 --
 * and the Overview painted EIGHT figures at that step above the fold.
 *
 * Measured against the five consoles the owner named as the target, that ratio
 * has no precedent. Koyeb's shipped design system tops out at 24px on 14px
 * body (1.71:1) and their Overview does not spend it on a number at all.
 * Vercel -- the one reference that does put a figure on a tile -- draws it at
 * roughly 1.1-1.25x its own label. Railway's project dashboard has no figure
 * on it whatsoever. So:
 *
 *   --t-figure  30 -> 22   1.57:1 against body, inside the reference band
 *   --t-title   20 -> 18   moved so the ladder keeps SIX DISTINCT steps: with
 *                          figure at 20 this would have read 12/13/14/16/20/20,
 *                          which is five steps wearing six names
 *
 * The floor, the count and the integer rule are unchanged, and the three tests
 * below that enforce them were not touched -- only these two values moved.
 * `--t-micro` through `--t-lead` are exactly as the owner set them.
 */
const SCALE: ReadonlyArray<readonly [string, string, string]> = [
  ['micro', '12px', '1.45'],
  ['meta', '13px', '1.45'],
  ['body', '14px', '1.50'],
  ['lead', '16px', '1.55'],
  ['title', '18px', '1.30'],
  ['figure', '22px', '1.10'],
]

/** The hard floor. Everything at 9.5, 10 and 10.5px moved UP to this. */
const FLOOR_PX = 12

/**
 * Every file that is allowed to say how big type is.
 *
 * Overview.tsx, HelpCard.tsx, HelpSection.tsx and AgentDetail.tsx are here
 * because they carried their own type -- Overview injected a stylesheet as a
 * template literal and the other three set React inline styles -- so scanning
 * only `styles.css` would have declared victory with 23 of the 240 still
 * shipping: 11 in Overview, 5 each in HelpCard and HelpSection, 2 in
 * AgentDetail. Overview's sheet is part of `styles.css` since U8; the file
 * stays in the list so an inline size written there is still caught.
 */
const SOURCES: ReadonlyArray<readonly [string, string]> = [
  ['src/styles.css', STYLES],
  ['src/Overview.tsx', OVERVIEW],
  ['src/HelpCard.tsx', HELP_CARD],
  ['src/HelpSection.tsx', HELP_SECTION],
  ['src/AgentDetail.tsx', AGENT_DETAIL],
]

/**
 * EVERY screen source, for the casing scans (CH-3). The size scan above names
 * its five files because those are the ones that carried type; casing can be
 * written into any component's inline style, so these read all of them.
 * `?raw` through Vite's own loader, as the imports above are.
 */
const SCREENS = import.meta.glob<string>(['../*.tsx', '../charts/*.tsx'], {
  query: '?raw',
  import: 'default',
  eager: true,
})

/**
 * Drop comments before anything reads the text.
 *
 * NOT OPTIONAL, and test_ui_contrast.py records the same lesson from the other
 * direction. The house style here is a long comment beside any value that was
 * argued over, and the §B4.1 comments necessarily QUOTE the declarations they
 * removed -- "`.ctl-metric-value { font-size: 20px }` IS GONE" is written in
 * four places. Without this step the migration's own explanation would fail
 * the migration.
 *
 * `//` is stripped only in the .tsx sources: a `//` inside styles.css would be
 * inside a url() or a comment, and stripping to end-of-line there could eat a
 * real declaration.
 *
 * NEWLINES ARE PRESERVED, so a reported line number is the line in the FILE.
 * Collapsing a 30-line comment to one space renumbers everything after it, and
 * a failure message that sends a reader to the wrong line is worse than one
 * that gives no line at all -- they go, find an unrelated rule, and conclude
 * the test is broken.
 */
function stripComments(src: string, isTsx: boolean): string {
  const blank = (m: string) => m.replace(/[^\n]/g, ' ')
  const noBlock = src.replace(/\/\*[\s\S]*?\*\//g, blank)
  return isTsx ? noBlock.replace(/^\s*\/\/.*$/gm, blank) : noBlock
}

/**
 * A raw pixel FONT SIZE, in any of the three spellings this codebase uses:
 *
 *   font-size: 13px            a CSS longhand
 *   fontSize: '13px'           a React inline style
 *   font: 600 13px/1.4 ...     a CSS shorthand, or its React string form
 *
 * The `--t-*` declarations in the token block are NOT matched and need no
 * exemption: they declare a custom property, not `font-size`, so the six px
 * literals that are allowed to exist are outside this pattern by construction
 * rather than by a carve-out somebody could widen. The floor test below is
 * what holds those six values to the scale.
 */
const RAW_SIZE = /(?:font-size|fontSize)\s*:\s*'?\s*[0-9.]+px|(?<![-\w])font\s*:\s*'?[^;'\n]*?[0-9.]+px/g

describe('B4.1: the scale is the only way to say how big type is', () => {
  // THE ASSERTION THAT STOPS THE 241st. Everything else in this file describes
  // the scale; this one is what makes it a scale rather than a suggestion.
  //
  // THERE ARE NO EXEMPTIONS. If you are about to add one, the bar is that the
  // element has a role none of the six steps names -- not that its old number
  // sat between two of them. Write the role and the reason here, next to the
  // exemption, the way every other value in this repository is written.
  it('leaves no raw px font size anywhere in the UI', () => {
    const found: string[] = []
    for (const [name, src] of SOURCES) {
      const clean = stripComments(src, name.endsWith('.tsx'))
      for (const m of clean.matchAll(RAW_SIZE)) {
        const line = clean.slice(0, m.index).split('\n').length
        found.push(`${name}:${line}  ${m[0].trim()}`)
      }
    }
    expect(
      found,
      `raw px font sizes are back. Each one belongs to a step of the scale --\n` +
        `pick it from what the element IS, not from which number is closest:\n` +
        found.join('\n'),
    ).toEqual([])
  })

  it('declares all six steps, each bound to its own line-height', () => {
    for (const [name, size, lh] of SCALE) {
      expect(STYLES, `--t-${name} is not declared`).toMatch(
        new RegExp(`--t-${name}:\\s*${size.replace('.', '\\.')}\\s*;`),
      )
      expect(STYLES, `--lh-${name} is not declared`).toMatch(
        new RegExp(`--lh-${name}:\\s*${lh.replace('.', '\\.')}\\s*;`),
      )
    }
  })

  it('holds the 11px floor: no step is smaller, and there is no seventh', () => {
    const declared = [...STYLES.matchAll(/--t-([a-z]+):\s*([0-9.]+)px\s*;/g)]
    // Exactly six. A seventh step is how seventeen sizes came back last time,
    // one defensible addition at a time.
    expect(declared.map((m) => m[1]).sort()).toEqual(
      SCALE.map(([n]) => n).sort(),
    )
    for (const m of declared) {
      expect(
        Number(m[2]),
        `--t-${m[1]} is ${m[2]}px, under the ${FLOOR_PX}px floor. The owner has ` +
          `said twice that this console is hard to read; nothing goes back below it.`,
      ).toBeGreaterThanOrEqual(FLOOR_PX)
    }
  })
})

describe('B5.2: casing is a rule, and the rule is that nothing shouts', () => {
  /**
   * THE OWNER'S QUESTION WAS "some helper text is all CAPS and others are
   * regular caps?? WHY?" and the answer was that there was no rule: TWENTY-TWO
   * declarations in `styles.css` forced `text-transform: uppercase`, each one
   * decided by whichever component was written that week, so labels of the
   * same RANK shouted on one screen and whispered on the next. Runtimes alone
   * rendered 93 uppercase elements.
   *
   * THE RULE (`styles.css` §B5.2): `text-transform` exists only to bring a
   * string the API chose into this console's register, never to emphasise one
   * we wrote. `lowercase`, `capitalize` and `none` all make a string quieter
   * or leave it alone; `uppercase` can only ever be emphasis, so it is the one
   * value banned outright.
   *
   * This is a source scan for the same reason the scan above it is: "no rule
   * in this sheet shouts" is a property of the TEXT, and `getComputedStyle`
   * can only be asked about elements that happen to be rendered — a rule on a
   * screen this suite never mounts is exactly where the twenty-third would
   * survive. The DOM half of the claim is in `brand.test.tsx`, which asserts
   * the three legal values are still reaching real elements.
   */
  it('writes no text-transform: uppercase anywhere in the sheet', () => {
    const clean = stripComments(STYLES, false)
    const shouting: string[] = []
    for (const m of clean.matchAll(/text-transform\s*:\s*uppercase/g)) {
      shouting.push(`src/styles.css:${clean.slice(0, m.index).split('\n').length}`)
    }
    expect(
      shouting,
      `uppercase is back. The label rank is MONO plus --text-faint (§2) --\n` +
        `two channels on one distinction. Uppercase was a third, and twenty-two\n` +
        `components never agreed on which labels qualified:\n${shouting.join('\n')}`,
    ).toEqual([])
  })

  /**
   * The tracking went with the capitals in the same edit, and this is what
   * stops it coming back alone. Letter-spacing on a label exists to open
   * CAPITALS up; on lowercase mono at --t-meta it reads as a loose word.
   *
   * NEGATIVE tracking is a different thing and is allowed: `--t-title` and
   * `.ctl-figure` both tighten, which is what large type needs. So the bound
   * is on positive values only, and `letter-spacing: 0` / `normal` (the two
   * spellings of "cancel an ancestor's") stay legal.
   */
  it('leaves no positive letter-spacing behind, which is the capitals' + "'" + ' tell', () => {
    const clean = stripComments(STYLES, false)
    const loose = [...clean.matchAll(/letter-spacing\s*:\s*(0?\.\d+em|[1-9][\d.]*(?:em|px))/g)].map(
      (m) => `src/styles.css:${clean.slice(0, m.index).split('\n').length}  ${m[0]}`,
    )
    expect(loose, `tracking without capitals to open up:\n${loose.join('\n')}`).toEqual([])
  })

  /**
   * THE SAME TWO RULES, IN THE SPELLING THE SCANS ABOVE COULD NOT READ (CH-3).
   *
   * Both scans read `styles.css` with kebab-case patterns, so a React inline
   * style -- `textTransform: 'uppercase'`, `letterSpacing: '.04em'` -- was
   * outside them by construction. The 2026-09-25 QA pass found exactly that:
   * every help card's title rendered in tracked capitals through
   * `HelpCard.tsx`'s `CARD_TITLE`, on the screens where §B5.2 said nothing
   * shouts. So every screen source is read here too, in BOTH spellings (a
   * `style="…"` string in a component is kebab-case), comments first.
   *
   * THE PENDING LIST, AND WHY IT IS NOT A FIX HERE. The two declarations in
   * `CARD_TITLE` are the fault this scan exists to catch, and removing them is
   * the markup's half of the box -- the `ui-shell-help-shared` lane's, written
   * in parallel in the same file as three other fixes to that card. They are
   * listed by file, by the object they are in and by their exact text, so the
   * allowance cannot excuse the same declaration anywhere else; the lane that
   * removes them deletes these two entries, and a stale entry is reported in
   * the log. Nothing may be ADDED to this list: a new finding fails.
   */
  const PENDING: ReadonlyArray<{ file: string; object: string; text: string }> = []
  const SHOUT_TSX = /textTransform\s*:\s*['"`]uppercase['"`]|text-transform\s*:\s*uppercase/g
  const TRACK_TSX = /letterSpacing\s*:\s*(?:['"`]\s*(?:0?\.\d+em|[1-9][\d.]*(?:em|px))\s*['"`]|[1-9][\d.]*)|letter-spacing\s*:\s*(?:0?\.\d+em|[1-9][\d.]*(?:em|px))/g

  /** Findings in the screen sources, with the ones on the pending list set aside. */
  function scanScreens(pattern: RegExp): { found: string[]; excused: string[] } {
    const found: string[] = []
    const excused: string[] = []
    for (const [path, src] of Object.entries(SCREENS)) {
      const file = path.replace(/^\.\.\//, '')
      const clean = stripComments(src, true)
      for (const m of clean.matchAll(pattern)) {
        const at = m.index ?? 0
        const where = `src/${file}:${clean.slice(0, at).split('\n').length}  ${m[0]}`
        const pending = PENDING.find((p) => {
          if (p.file !== file || p.text !== m[0]) return false
          const open = clean.indexOf(`const ${p.object}`)
          const close = open === -1 ? -1 : clean.indexOf('}', open)
          return open !== -1 && at > open && at < close
        })
        if (pending === undefined) found.push(where)
        else excused.push(where)
      }
    }
    return { found, excused }
  }

  it('reads the screen sources it claims to, and its patterns find both spellings', () => {
    // The precondition, so neither scan below can pass over an empty glob.
    expect(Object.keys(SCREENS).length, 'the glob read no screen sources').toBeGreaterThan(20)
    // And the patterns against a known answer: the defect's own spelling, a
    // component's style string, and the legal values that must not match.
    const sample = [
      "const T: CSSProperties = { textTransform: 'uppercase', letterSpacing: '.04em' }",
      '<span style="text-transform: uppercase; letter-spacing: 1px">',
      "const U = { textTransform: 'lowercase', letterSpacing: 0, letterSpacing: '-0.01em' }",
    ].join('\n')
    expect(sample.match(SHOUT_TSX)?.length).toBe(2)
    expect(sample.match(TRACK_TSX)?.length).toBe(2)
  })

  it('writes no uppercase and no positive tracking in any screen\'s inline styles', () => {
    const shout = scanScreens(SHOUT_TSX)
    const track = scanScreens(TRACK_TSX)
    const stale = PENDING.filter(
      (p) => ![...shout.excused, ...track.excused].some((e) => e.includes(p.file) && e.endsWith(p.text)),
    )
    if (stale.length > 0) {
      console.log(
        `typescale: PENDING entries that match nothing any more -- delete them:\n` +
          stale.map((p) => `  ${p.file} ${p.object} ${p.text}`).join('\n'),
      )
    }
    expect(shout.found, `inline uppercase in a screen:\n${shout.found.join('\n')}`).toEqual([])
    expect(track.found, `inline positive tracking in a screen:\n${track.found.join('\n')}`).toEqual([])
  })
})

describe('B4.1: the line-height travels with the size', () => {
  // A `font:` shorthand that names a size and NOT a line-height resets
  // line-height to `normal` -- roughly 1.2, and the browser's number rather
  // than ours. That is how vertical rhythm was impossible before: the leading
  // was whatever each rule happened to omit. So the pairing is enforced, not
  // hoped for.
  it('every font: shorthand carrying a size token carries a line-height token', () => {
    const clean = stripComments(STYLES, false)
    const bad: string[] = []
    for (const m of clean.matchAll(/(?<![-\w])font\s*:\s*[^;{}]*var\(--t-[a-z]+\)[^;{}]*/g)) {
      const decl = m[0]
      const step = /var\(--t-([a-z]+)\)/.exec(decl)![1]
      const lh = /var\(--t-[a-z]+\)\s*\/\s*var\(--lh-([a-z]+)\)/.exec(decl)
      if (lh === null) {
        bad.push(`no /var(--lh-*):  ${decl.trim()}`)
      } else if (lh[1] !== step && lh[1] !== 'flush') {
        // --lh-flush is the one legal cross-pairing, and the sheet names the
        // box it is protecting at every use of it.
        bad.push(`--t-${step} paired with --lh-${lh[1]}:  ${decl.trim()}`)
      }
    }
    expect(bad, bad.join('\n')).toEqual([])
  })

  // The same pairing, in the other spelling. `.section > h2` and the React
  // inline styles are written as longhands -- deliberately, so jsdom can read
  // them back -- which puts the size and its leading in two declarations that
  // nothing but this holds together.
  it('every longhand font-size sits with its own line-height, or with none', () => {
    const clean = stripComments(STYLES, false)
    const bad: string[] = []
    // Innermost blocks only: the pattern excludes braces, so an @media
    // wrapper never matches as one rule -- the same trick test_ui_contrast.py
    // uses to walk this sheet.
    for (const m of clean.matchAll(/\{([^{}]*)\}/g)) {
      const body = m[1] ?? ''
      const size = /(?<![-\w])font-size\s*:\s*var\(--t-([a-z]+)\)/.exec(body)?.[1]
      const lh = /(?<![-\w])line-height\s*:\s*var\(--lh-([a-z]+)\)/.exec(body)?.[1]
      if (size === undefined || lh === undefined) continue
      if (lh !== size && lh !== 'flush') {
        bad.push(`--t-${size} with --lh-${lh}:  ${body.split('\n').join(' ').trim()}`)
      }
    }
    expect(bad, bad.join('\n')).toEqual([])
  })

  it('writes no raw line-height anywhere: leading is part of a step', () => {
    const clean = stripComments(STYLES, false)
    const raw = [...clean.matchAll(/(?<![-\w])line-height\s*:\s*([^;}]+)/g)]
      .map((m) => (m[1] ?? '').trim())
      .filter((v) => !v.startsWith('var(--lh-'))
    // A hand-picked 1.6 next to a token size is the rhythm leaking back out
    // one rule at a time, which is how seventeen sizes happened.
    expect(raw, `raw line-heights: ${raw.join(', ')}`).toEqual([])
  })

  it('declares --lh-flush as 1, and nothing else claims to be a step', () => {
    expect(STYLES).toMatch(/--lh-flush:\s*1\s*;/)
    // It is a line-height, never a size: `--t-flush` would be a seventh step
    // wearing a different name.
    expect(STYLES).not.toMatch(/--t-flush\s*:/)
  })
})

describe('B4.1: the tokens are actually in the cascade', () => {
  /**
   * Put the SHIPPED stylesheet into the document, the way shell.test.tsx and
   * brand.test.tsx do. A hand-written copy of the rules under test would be
   * the fixture-that-proves-nothing problem.
   */
  function withStyles(): HTMLStyleElement {
    const el = document.createElement('style')
    el.textContent = STYLES
    document.head.appendChild(el)
    return el
  }

  it('resolves every step off :root to the value the scale states', () => {
    const style = withStyles()
    const root = getComputedStyle(document.documentElement)
    for (const [name, size, lh] of SCALE) {
      // Custom properties DO resolve in jsdom, because these values are
      // literals rather than `var()` references. This is the half of the file
      // that cannot pass on an empty stylesheet.
      expect(root.getPropertyValue(`--t-${name}`).trim(), `--t-${name}`).toBe(size)
      expect(root.getPropertyValue(`--lh-${name}`).trim(), `--lh-${name}`).toBe(lh)
    }
    style.remove()
  })

  it('leaves --row-h at 30px, which is --t-body at --lh-body plus 8', () => {
    // The owner picked 30px after seeing it rendered, so the scale was built
    // around it rather than over it: 13 x 1.5 = 19.5, plus 4px above and below
    // in `.ctl-table td`, is 27.5 -- inside a 30px row. If --t-body ever moves,
    // this is the assertion that says --row-h has to be re-decided by a person.
    const style = withStyles()
    const root = getComputedStyle(document.documentElement)
    expect(root.getPropertyValue('--row-h').trim()).toBe('30px')
    const body = Number(root.getPropertyValue('--t-body').trim().replace('px', ''))
    const lh = Number(root.getPropertyValue('--lh-body').trim())
    expect(body * lh + 8).toBeLessThanOrEqual(30)
    style.remove()
  })

  it('fixes the inversion: a panel title is --t-title in --text, not small caps', () => {
    const style = withStyles()
    const section = document.createElement('section')
    section.className = 'section'
    const h2 = document.createElement('h2')
    h2.textContent = 'Runner profiles'
    section.appendChild(h2)
    document.body.appendChild(section)

    // jsdom answers with the literal token, which is the point: it proves the
    // rule is in the cascade AND that it carries the token rather than a
    // number. `.section > h2` is written as longhands precisely so this can be
    // read back -- a `font:` shorthand holding var() is not expanded here.
    const seen = getComputedStyle(h2)
    expect(seen.fontSize).toBe('var(--t-title)')
    expect(seen.lineHeight).toBe('var(--lh-title)')
    expect(seen.color).toBe('var(--text)')
    // It was 13px, 600, uppercase, in --text-faint: a heading drawn smaller
    // and fainter than the rows under it, losing to its own content on size,
    // weight and tone at once. The uppercase treatment dropped one level, to
    // panel labels -- see `.ov-h > h2` and the `h4`s in a detail panel.
    expect(seen.textTransform).not.toBe('uppercase')

    section.remove()
    style.remove()
  })
})
