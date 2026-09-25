// THE PRODUCT FRAME, AS BEHAVIOUR: the mark, the header, the environment, and
// the two frame-level defects (B17, B19).
//
// WHY THESE ARE RENDERED ASSERTIONS AND NOT SOURCE GREPS. A verifier on an
// earlier wave neutered a guard in this app with `false &&` and the suite
// stayed green, because the test only checked that a string appeared in the
// SOURCE. Every claim below is made against a DOM this app actually produced,
// or against the computed style the real `styles.css` actually resolves to.
//
// THE ONE THING THAT IS NOT A DOM ASSERTION, and why. jsdom implements the
// cascade but not layout and not media-query evaluation: `getComputedStyle`
// reads `.app`'s declared `max-width` and resolves `--app-max` off `:root`,
// but a `@media (min-width: 1600px)` block never applies because there is no
// viewport to match it against. So the wide breakpoints are asserted through
// the CSSOM -- `CSSMediaRule.conditionText` and the declarations inside it,
// parsed by jsdom's own CSS parser -- rather than by measuring a rendered
// width, which nothing in this repository is able to do offline. That is a
// real limitation and it is named here rather than papered over: what a
// breakpoint LOOKS like at 1600px is not verified by this file.

// `?raw` rather than `node:fs`: it is Vite's own loader, it needs no
// `@types/node`, and it resolves the same path the bundle would, so a moved
// file fails to resolve here instead of reading a stale copy from disk.
import STYLES from '../styles.css?raw'
import INDEX_HTML from '../../index.html?raw'
import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import {
  COMPACT,
  EnvironmentBadge,
  MARK_COMPACT_MAX,
  ProductHeader,
  SwarmMark,
  classifyEnvironment,
  envTreatment,
  type Environment,
} from '../Brand'
import { App } from '../App'
import { Screen } from '../Shell'
import { WorkflowsScreen } from '../Workflows'
import type { Me } from '../types'
import { flatRules, type CascadeEnv } from './cssgate'
import { painted } from './marks'

/**
 * Put the SHIPPED stylesheet into the document so `getComputedStyle` answers
 * with the real cascade. Vitest does not process `styles.css` for a test file
 * (no `css: true`), and a hand-written copy of the rule under test would be
 * the fixture-that-proves-nothing problem, so the file itself is read.
 */
function withStyles(): HTMLStyleElement {
  const el = document.createElement('style')
  el.textContent = STYLES
  document.head.appendChild(el)
  return el
}

function me(over: Partial<Me['principal']> = {}): Me {
  return {
    tenant: {
      tenant_id: 'u-bogdan',
      kind: 'user',
      principal: 'someone@saga.xyz',
      display_name: 'Bogdan',
      created_at: new Date().toISOString(),
      max_active: 4,
      capacity_units: 8,
      monthly_budget_usd: null,
      enabled: true,
      credentials: ['anthropic'],
      service_account: 'swarm-agent-worker-u-bogdan@example.iam.gserviceaccount.com',
      gcs_prefix: 'tenants/u-bogdan',
      namespace: 'swarm-u-bogdan',
    },
    principal: {
      email: 'someone@saga.xyz',
      domain: 'saga.xyz',
      groups: [],
      is_admin: false,
      ...over,
    },
    // UNDECLARED, so every badge case in this file still reads the build and
    // the host it passes. The API's own environment, and what it outranks, is
    // brand.environment.test.tsx's subject.
    environment: 'dev',
    environment_declared: false,
  }
}

// ---------------------------------------------------------------------------
// (a) The mark
// ---------------------------------------------------------------------------

describe('the SwarmCloud mark', () => {
  it('drops every stroke below the size at which strokes stop reading', () => {
    const { container } = render(<SwarmMark size={16} />)
    const svg = container.querySelector('svg')!

    // Seven discs: six agents on the ring, one co-ordinator in the middle.
    expect(svg.querySelectorAll('circle')).toHaveLength(7)
    // AND NOTHING ELSE. At 16px a 1px spoke and a 1.1px hex outline land on
    // the same pixels as the discs; the rasterisation at 14/16/18/20/24/28 px
    // is what set this threshold, and the compact variant is the one that
    // survives it.
    expect(svg.querySelectorAll('line')).toHaveLength(0)
    expect(svg.querySelectorAll('polygon')).toHaveLength(0)
  })

  it('draws the full mesh once there is room for it', () => {
    const { container } = render(<SwarmMark size={28} />)
    const svg = container.querySelector('svg')!
    expect(svg.querySelectorAll('circle')).toHaveLength(7)
    // Six spokes from the centre and the hexagon they end on: the part that
    // says "coordinated" rather than merely "several".
    expect(svg.querySelectorAll('line')).toHaveLength(6)
    expect(svg.querySelectorAll('polygon')).toHaveLength(1)
  })

  it('switches at exactly the measured threshold', () => {
    // The constant is pinned to a band, not to a number, because it came from
    // looking at rasterisations and someone re-measuring should be able to
    // move it by a pixel. Moving it far enough to matter -- to 0, say, so the
    // favicon becomes the full mesh -- fails here and in the two tests above.
    expect(MARK_COMPACT_MAX).toBeGreaterThanOrEqual(18)
    expect(MARK_COMPACT_MAX).toBeLessThanOrEqual(26)

    const under = render(<SwarmMark size={MARK_COMPACT_MAX - 1} />)
    expect(under.container.querySelectorAll('line')).toHaveLength(0)
    const at = render(<SwarmMark size={MARK_COMPACT_MAX} />)
    expect(at.container.querySelectorAll('line')).toHaveLength(6)
  })

  it('paints only in currentColor, which is how it works on both grounds', () => {
    // THE LIGHT/DARK CLAIM, AS A PROPERTY OF THE OUTPUT. A hardcoded hex is
    // the only way this mark can be unreadable on a ground that the
    // surrounding text is readable on. There is none, in either variant, so
    // there is no ground to check separately.
    for (const size of [16, 28]) {
      const { container } = render(<SwarmMark size={size} />)
      const markup = container.innerHTML
      expect(markup, `size ${size} carries a literal colour`).not.toMatch(/#[0-9a-fA-F]{3,8}/)
      expect(markup).toContain('currentColor')
    }
  })

  it('is announced when it is the only thing naming the product, and silent otherwise', () => {
    const named = render(<SwarmMark size={28} title="SwarmCloud" />)
    expect(named.container.querySelector('svg')!.getAttribute('aria-label')).toBe('SwarmCloud')
    const plain = render(<SwarmMark size={16} />)
    expect(plain.container.querySelector('svg')!.getAttribute('aria-hidden')).toBe('true')
  })

  it('and the favicon in index.html is the SAME mark, disc for disc', () => {
    // ANTI-DRIFT. The favicon cannot import the component -- it is a data URI
    // in a static HTML file -- so the only thing stopping the two copies
    // diverging is this assertion. It renders the component and compares the
    // geometry, so moving a disc in Brand.tsx and forgetting index.html fails
    // here rather than shipping two different logos.
    const href = /rel="icon"[\s\S]*?href="([^"]+)"/.exec(INDEX_HTML)?.[1]
    expect(href, 'index.html declares no icon').toBeTruthy()
    const svg = decodeURIComponent(href!.replace(/^data:image\/svg\+xml,/, ''))

    const faviconCircles = [...svg.matchAll(/<circle cx='([\d.]+)' cy='([\d.]+)' r='([\d.]+)'/g)].map(
      (m) => `${m[1]},${m[2]},${m[3]}`,
    )

    const { container } = render(<SwarmMark size={16} />)
    const componentCircles = [...container.querySelectorAll('circle')].map(
      (c) => `${c.getAttribute('cx')},${c.getAttribute('cy')},${c.getAttribute('r')}`,
    )

    expect(faviconCircles).toEqual(componentCircles)
    // And it is the compact geometry, not some third set of numbers.
    expect(componentCircles).toContain(`12,12,${COMPACT.core}`)
  })

  it('and the favicon carries its own answer for a dark tab bar', () => {
    // A favicon has no page to inherit `currentColor` from, so the one thing
    // it cannot do is what the component does. It answers with a media query
    // instead; without this the mark is invisible on one of the two tab bars.
    const href = /rel="icon"[\s\S]*?href="([^"]+)"/.exec(INDEX_HTML)![1]!
    const svg = decodeURIComponent(href.replace(/^data:image\/svg\+xml,/, ''))
    expect(svg).toContain('prefers-color-scheme:dark')
    expect(svg).toMatch(/fill:#[0-9a-f]{6}/)
  })
})

// ---------------------------------------------------------------------------
// (b) The environment, which is a safety property
// ---------------------------------------------------------------------------

describe('which environment this console is pointed at', () => {
  it('says so when the build declared it', () => {
    expect(classifyEnvironment('dev', 'swarm.saga.xyz')).toEqual({
      kind: 'nonprod',
      name: 'dev',
      source: 'build',
    })
    expect(classifyEnvironment('PROD', 'swarm.saga.xyz').kind).toBe('production')
    expect(classifyEnvironment('production', 'x').kind).toBe('production')
    // A suffixed production name is still production. This is the case a
    // straight equality check gets wrong, and getting it wrong means a quiet
    // badge over a real cluster.
    expect(classifyEnvironment('prod-eu', 'x').kind).toBe('production')
    expect(classifyEnvironment('staging', 'x').kind).toBe('nonprod')
  })

  it('recognises this machine without being told', () => {
    for (const h of ['localhost', '127.0.0.1', '::1', 'swarm.local', 'foo.localhost']) {
      expect(classifyEnvironment(undefined, h).kind, h).toBe('local')
    }
  })

  it('REFUSES TO GUESS, and says so loudly instead', () => {
    // The whole point. Nothing declared it, the host is not this machine, and
    // the console does not know. The previous behaviour was a hardcoded "dev"
    // on sixteen screens, which is the same sentence with the refusal removed.
    const env = classifyEnvironment(undefined, 'swarm.saga.xyz')
    expect(env).toEqual({ kind: 'unknown', host: 'swarm.saga.xyz' })

    const t = envTreatment(env)
    expect(t.label).toBe('ENVIRONMENT UNKNOWN')
    // Not knowing is treated like production, not like dev: the bar is drawn.
    expect(t.bar).toBe(true)
    expect(t.explain).toContain('treat it as production')
  })

  it('draws the bar for exactly the two kinds where a mistake is expensive', () => {
    expect(envTreatment(classifyEnvironment('prod', 'x')).bar).toBe(true)
    expect(envTreatment(classifyEnvironment(undefined, 'x.example')).bar).toBe(true)
    expect(envTreatment(classifyEnvironment('dev', 'x')).bar).toBe(false)
    expect(envTreatment(classifyEnvironment(undefined, 'localhost')).bar).toBe(false)
  })

  it('tells the four apart WITHOUT USING COLOUR', () => {
    // The stylesheet's own acceptance rule for the state palette, applied to
    // the environment: render it in greyscale and two things that mean
    // different things must still look different. So the tuple compared here
    // deliberately leaves the colour out and keeps only shape, border style
    // and whether the surface is hatched.
    const style = withStyles()
    const envs: Environment[] = [
      { kind: 'production', name: 'prod', source: 'build' },
      { kind: 'nonprod', name: 'dev', source: 'build' },
      { kind: 'local', name: 'local', source: 'host', host: 'localhost' },
      { kind: 'unknown', host: 'swarm.example' },
    ]

    const shapes = envs.map((env) => {
      const { container } = render(<EnvironmentBadge env={env} />)
      const badge = container.querySelector('.brand-env')!
      const s = getComputedStyle(badge)
      const bg = s.background || ''
      return [s.borderRadius, s.borderStyle || s.borderTopStyle, bg.includes('hatch') ? 'hatched' : 'flat'].join('|')
    })

    expect(new Set(shapes).size, `two environments render the same mark: ${shapes.join(' / ')}`).toBe(
      envs.length,
    )
    style.remove()
  })
})

// ---------------------------------------------------------------------------
// The badge's casing -- an owner decision, 2026-09-24 (design-system.md §13.6)
// ---------------------------------------------------------------------------

/** Every cased letter is a capital, and there is at least one. */
function isCapitals(s: string): boolean {
  return s === s.toUpperCase() && s !== s.toLowerCase()
}

/**
 * A capital first letter and nothing shouted after it. The first letter has to
 * be a real capital (`toLowerCase` changes it), so an empty label -- a badge
 * that printed nothing -- is not sentence case by default.
 */
function isSentenceCase(s: string): boolean {
  const head = s.charAt(0)
  const rest = s.slice(1)
  return (
    head !== head.toLowerCase() &&
    head === head.toUpperCase() &&
    rest === rest.toLowerCase() &&
    !isCapitals(s)
  )
}

/**
 * Declarations that make text shout. Uppercase is the one §13.2 bans outright.
 * Small caps is the same emphasis drawn smaller, so it counts too.
 */
const SHOUTS: ReadonlyArray<readonly [string, RegExp]> = [
  ['text-transform', /\buppercase\b/i],
  ['font-variant-caps', /\b(?:all-)?(?:small|petite)-caps\b|\bunicase\b|\btitling-caps\b/i],
  ['font-variant', /\b(?:all-)?(?:small|petite)-caps\b|\bunicase\b|\btitling-caps\b/i],
  ['font', /\bsmall-caps\b/i],
]

/** A pseudo-element that paints a box of its own, not the element's text. */
const OTHER_BOX = /::?(?:before|after|marker|placeholder|selection|backdrop|file-selector-button|-webkit-[\w-]+|-moz-[\w-]+)\b/i
/** Pseudo-elements that style the element's own text: they reach it. */
const OWN_TEXT = /::?(?:first-line|first-letter)\b/gi
/** State the test cannot be in. A shout on `:hover` is still a shout. */
const DYNAMIC = /:(?:hover|focus-visible|focus-within|focus|active|visited|target)\b/g

/** A selector list split at top-level commas: `:where(a, b)` is one selector. */
function splitSelectors(list: string): string[] {
  const out: string[] = []
  let depth = 0
  let quote = ''
  let buf = ''
  for (const ch of list) {
    if (quote) {
      if (ch === quote) quote = ''
    } else if (ch === '"' || ch === "'") {
      quote = ch
    } else if (ch === '(' || ch === '[') {
      depth += 1
    } else if (ch === ')' || ch === ']') {
      depth -= 1
    } else if (ch === ',' && depth === 0) {
      out.push(buf.trim())
      buf = ''
      continue
    }
    buf += ch
  }
  if (buf.trim()) out.push(buf.trim())
  return out
}

type AnyRule = CSSRule & {
  selectorText?: string
  style?: CSSStyleDeclaration
  cssRules?: CSSRuleList
  media?: MediaList
  conditionText?: string
}

/**
 * Every rule in every sheet in the document that could make `el` shout,
 * applied to `el` itself or to any ancestor it inherits casing from.
 *
 * NOT `getComputedStyle`, AND WHY. jsdom 25 resolves inheritance only for a
 * short list of properties (`visibility`, `pointer-events`, the colours), and
 * `text-transform` is not on it. It applies no `@media` block unless the
 * block names `screen`. It orders the cascade by source position alone,
 * ignoring specificity. So the computed value reads `''` under an ancestor
 * rule or a breakpoint rule that shouts in a browser. This asks each rule
 * directly, with jsdom's own selector engine (`Element.matches`), inside every
 * `@media` / `@supports` block. A selector it cannot evaluate is reported
 * rather than skipped.
 *
 * `reached` counts every rule that matched something on the chain, shouting
 * or not, so an empty result can be told apart from a walk that read nothing.
 */
function shoutingRules(el: Element): { hits: string[]; reached: number } {
  const chain: Element[] = []
  for (let n: Element | null = el; n !== null; n = n.parentElement) chain.push(n)
  const nameOf = (n: Element) =>
    `<${n.tagName.toLowerCase()}${n.className ? ` class="${n.className}"` : ''}>`

  const hits: string[] = []
  let reached = 0
  const visit = (list: CSSRuleList, where: string) => {
    for (const rule of Array.from(list) as AnyRule[]) {
      if (rule.selectorText === undefined) {
        if (rule.cssRules) {
          const cond = rule.media?.mediaText ?? rule.conditionText ?? ''
          visit(rule.cssRules, `${where} @${cond}`.trim())
        }
        continue
      }
      const shouts = SHOUTS.flatMap(([prop, re]) => {
        const value = rule.style?.getPropertyValue(prop) ?? ''
        return re.test(value) ? [`${prop}: ${value}`] : []
      })
      for (const selector of splitSelectors(rule.selectorText)) {
        if (OTHER_BOX.test(selector)) continue
        const probe = selector.replace(OWN_TEXT, '').replace(DYNAMIC, '')
        for (const node of chain) {
          let matched: boolean
          try {
            matched = node.matches(probe)
          } catch {
            if (shouts.length > 0) {
              hits.push(
                `${selector} { ${shouts.join('; ')} }${where ? ` in ${where}` : ''}: ` +
                  'could not be evaluated, so cannot be cleared',
              )
            }
            break
          }
          if (!matched) continue
          reached += 1
          if (shouts.length > 0) {
            hits.push(
              `${selector} { ${shouts.join('; ')} }${where ? ` in ${where}` : ''} reaches ${nameOf(node)}`,
            )
          }
        }
      }
    }
  }
  for (const sheet of Array.from(document.styleSheets)) visit(sheet.cssRules, '')
  for (const node of chain) {
    const inline = (node as HTMLElement).style
    if (!inline) continue
    for (const [prop, re] of SHOUTS) {
      const value = inline.getPropertyValue(prop)
      if (re.test(value)) hits.push(`inline ${prop}: ${value} on ${nameOf(node)}`)
    }
  }
  return { hits, reached }
}

/** What the badge actually PRINTS, read off a render rather than off `envTreatment`. */
function printed(env: Environment): string {
  const { container, unmount } = render(<EnvironmentBadge env={env} />)
  const text = container.querySelector('.brand-env-name')?.textContent ?? ''
  unmount()
  return text
}

describe('the badge shouts only where shouting is a safety signal', () => {
  // THE DECISION. §13.2 bans emphasis on any string this console authors, and
  // the badge was the one place still doing it -- `env.name.toUpperCase()` for
  // every declared environment and a literal `'LOCAL'`. The owner's ruling:
  // `dev` and `local` render in sentence case like everything else; the
  // PRODUCTION banner and ENVIRONMENT UNKNOWN keep their capitals, because
  // those two are safety signals and not typography.
  //
  // PINNED AS A PROPERTY OVER CLASSIFIED INPUTS, NOT AS A LIST OF STRINGS.
  // Every environment below goes through `classifyEnvironment` first, so a
  // new name that classifies as production is held to capitals and a new
  // quiet one to sentence case without anyone editing this file.

  const loud: Environment[] = [
    classifyEnvironment('prod', 'x'),
    classifyEnvironment('production', 'x'),
    classifyEnvironment('prod-eu', 'x'),
    classifyEnvironment('live', 'x'),
    classifyEnvironment(undefined, 'swarm.saga.xyz'),
    classifyEnvironment(undefined, ''),
  ]
  const quiet: Environment[] = [
    classifyEnvironment('dev', 'x'),
    classifyEnvironment('DEV', 'x'),
    classifyEnvironment('staging', 'x'),
    classifyEnvironment('qa-2', 'x'),
    classifyEnvironment(undefined, 'localhost'),
    classifyEnvironment(undefined, 'swarm.local'),
  ]

  it('keeps production and unknown in capitals', () => {
    for (const env of loud) {
      const label = printed(env)
      expect(isCapitals(label), `${env.kind} printed "${label}", which is not a safety signal`).toBe(
        true,
      )
    }
  })

  it('writes every other environment in sentence case', () => {
    for (const env of quiet) {
      const label = printed(env)
      expect(isCapitals(label), `${env.kind} printed "${label}" in capitals`).toBe(false)
      expect(isSentenceCase(label), `${env.kind} printed "${label}", not sentence case`).toBe(true)
    }
  })

  it('spends capitals exactly where the header draws the bar', () => {
    // The two safety channels have to agree: a badge that shouts without the
    // bar, or draws the bar in a whisper, is one of them saying the wrong
    // thing. `bar` is already pinned to production and unknown above.
    for (const env of [...loud, ...quiet]) {
      const t = envTreatment(env)
      expect(isCapitals(printed(env)), `${env.kind}: capitals and bar disagree`).toBe(t.bar)
    }
  })

  it('and no rule in any sheet makes it shout, on the label or on anything it inherits from', () => {
    // The casing is decided in the source, so CSS that shouts would undo the
    // decision above without touching Brand.tsx.
    //
    // WHAT MOVED, AND WHY. This used to read
    // `getComputedStyle(label).textTransform` off a badge rendered on its own,
    // and said that meant it "does not lean on a different test's grep". It
    // did lean on one. jsdom 25 does not inherit `text-transform`, and it
    // applies no `@media` block that does not name `screen`. Mutation commit
    // 232921d added `.brand-row { text-transform: uppercase }` and
    // `@media (max-width: 560px) { .brand-env-name { text-transform: uppercase } }`.
    // In CI run 35977623224 this test passed on both, and only
    // typescale.test.ts's sheet grep went red.
    //
    // So this renders the REAL frame (`App`: `.ctl-frame > .ctl-scroll >
    // header.brand > .brand-row > .brand-env`) and asks every rule in every
    // sheet in the document, at every breakpoint, whether it reaches the label
    // or any ancestor and shouts (`shoutingRules`). That includes the shipped
    // `styles.css` and any sheet a screen has injected by then.
    //
    // It is strict: a shouting rule on an ancestor fails even if a nearer
    // `none` would override it. §13.2 leaves no legal shouting rule to
    // override, so strict costs nothing.
    const style = withStyles()
    render(<App />)
    const label = document.querySelector('.brand-env-name')
    expect(label, 'the frame rendered no environment badge').toBeTruthy()
    // Walking the real frame, not a badge on its own, is what makes the
    // ancestor half of this claim true.
    expect(label!.closest('.ctl-frame'), 'the badge was not rendered inside the app frame').toBeTruthy()

    const { hits, reached } = shoutingRules(label!)
    expect(
      reached,
      'no rule in any sheet matched the badge or any ancestor, so the walk read nothing',
    ).toBeGreaterThan(0)
    expect(hits, `CSS makes the environment badge shout:\n  ${hits.join('\n  ')}`).toEqual([])
    style.remove()
  })
})

// ---------------------------------------------------------------------------
// The header
// ---------------------------------------------------------------------------

describe('the product header', () => {
  it('carries the logo, the wordmark, the environment and who you are', async () => {
    render(
      <ProductHeader
        load={async () => ({ status: 'ok', data: me(), fetchedAt: Date.now() })}
        declaredEnv="dev"
        host="localhost"
      />,
    )
    expect(document.querySelector('.brand-mark')).toBeTruthy()
    expect(document.querySelector('.brand-word')?.textContent).toBe('SwarmCloud')
    // WHAT MOVED: this pinned the literal 'DEV'. The owner decided on
    // 2026-09-24 (design-system.md §13.6) that a quiet environment is written
    // in sentence case like every other string this console authors, so what
    // is pinned now is that the badge NAMES dev and does not SHOUT it -- the
    // casing rule itself is the describe block below.
    const name = document.querySelector('.brand-env-name')?.textContent ?? ''
    expect(name.toLowerCase()).toBe('dev')
    expect(name, 'a non-production badge is shouting').not.toBe(name.toUpperCase())
    expect(await screen.findByText('u-bogdan')).toBeTruthy()
    expect(screen.getByText('someone@saga.xyz')).toBeTruthy()
  })

  it('never renders an identity it has not read', async () => {
    // THE HONESTY RULE, IN THE FRAME. A header that leaves the tenant slot
    // blank while `/v1/tenants/me` is failing is this platform's defining bug
    // in the one place every screen shows.
    render(
      <ProductHeader
        load={async () => ({
          status: 'error',
          error: {
            kind: 'session_expired',
            httpStatus: 401,
            code: 'unauthenticated',
            message: 'The IAP assertion had expired.',
          },
        })}
        declaredEnv="dev"
        host="localhost"
      />,
    )
    const who = await waitFor(() => {
      const el = document.querySelector('.brand-who.is-unread')
      expect(el).toBeTruthy()
      return el!
    })
    // RE-POINTED BY CH-20: the slot is the tenant key and the kit's `not read`
    // mark, and the error heading is the slot's accessible name rather than a
    // sentence in a 52px bar.
    expect(who.querySelector('.brand-k')?.textContent).toBe('tenant')
    expect(who.querySelector('.ctl-mark.is-unread')?.textContent).toBe('not read')
    expect(who.getAttribute('aria-label') ?? '').toContain('Your session expired')
    // And no invented tenant anywhere in the bar.
    expect(document.querySelector('.brand-who .id')).toBeNull()
  })

  it('says it is still reading rather than showing an empty slot', () => {
    render(<ProductHeader load={() => new Promise(() => {})} declaredEnv="dev" host="localhost" />)
    // RE-POINTED BY CH-20: the tenant key, then "reading…" -- CH-2's word for
    // a read in flight, at every width. It was the sentence "Reading who you
    // are…", which is what doubled the header's height at 390.
    const pending = document.querySelector('.brand-who.is-pending')
    expect(pending?.querySelector('.brand-k')?.textContent).toBe('tenant')
    expect(pending?.textContent).toContain('reading…')
    expect(document.querySelector('.brand-who .id')).toBeNull()
  })

  it('marks an admin, because /v1/admin answering is a fact about you', async () => {
    render(
      <ProductHeader
        load={async () => ({ status: 'ok', data: me({ is_admin: true }), fetchedAt: Date.now() })}
        declaredEnv="dev"
        host="localhost"
      />,
    )
    expect(await screen.findByText('admin')).toBeTruthy()
  })

  it('prints NO environment word it did not measure', () => {
    render(
      <ProductHeader
        load={() => new Promise(() => {})}
        declaredEnv={undefined}
        host="swarm.saga.xyz"
      />,
    )
    const badge = document.querySelector('.brand-env')!
    // Scoped to the badge on purpose: an address elsewhere in the bar may
    // legitimately contain the letters "dev".
    expect(badge.textContent).not.toMatch(/\bdev\b/i)
    expect(badge.textContent).toContain('ENVIRONMENT UNKNOWN')
    // The host it could not classify is printed, so the reader can act on it.
    expect(badge.textContent).toContain('swarm.saga.xyz')
    expect(document.querySelector('.brand-bar')).toBeTruthy()
  })

  it('and no screen prints one of its own any more', async () => {
    // Sixteen screens used to draw a hardcoded `dev` beside their own title
    // through this component. A second opinion about the environment is worse
    // than none: it is the one next to what you are reading.
    render(
      <Screen<number[]> title="Agents" load={async () => ({ status: 'ok', data: [1], fetchedAt: Date.now() })}>
        {(d) => <span>{d.length}</span>}
      </Screen>,
    )
    await screen.findByText('1')
    expect(document.querySelector('.head .env')).toBeNull()
    expect(document.querySelector('.head')!.textContent).toBe('Agents')
  })
})

// ---------------------------------------------------------------------------
// CH-20 -- at 560px and below the header is one row
// ---------------------------------------------------------------------------
//
// At 390 the header doubled to about 100px and the admin badge wrapped alone
// onto a third line, against design-system.md §7.2's "the header keeps its
// height". These are asked of the CASCADE at a stated width (cssgate.ts),
// because jsdom applies no media query.

const PHONE_W: CascadeEnv = { width: 390 }
const MID_W: CascadeEnv = { width: 720 }
const WIDE_W: CascadeEnv = { width: 1440 }

/** Not drawn, but still in the accessibility tree: `display: none` is not this. */
function visuallyHidden(el: Element, env: CascadeEnv): boolean {
  return (
    painted(el, 'clip-path', env) === 'inset(50%)' &&
    painted(el, 'width', env) === '1px' &&
    painted(el, 'overflow', env) === 'hidden'
  )
}

/** Not drawn by any means. */
function notDrawn(el: Element, env: CascadeEnv): boolean {
  return painted(el, 'display', env) === 'none' || visuallyHidden(el, env)
}

describe('CH-20: at 560px and below the product header is one row', () => {
  async function admin(): Promise<void> {
    render(
      <ProductHeader
        load={async () => ({ status: 'ok', data: me({ is_admin: true }), fetchedAt: Date.now() })}
        declaredEnv="dev"
        host="localhost"
      />,
    )
    await screen.findByText('admin')
  }

  it('keeps the signed-in principal and the admin tag in one unit that never wraps', async () => {
    // So above 560px "admin" cannot wrap onto a line of its own.
    // MUTATION: render the tag outside `.brand-who-me`, or let the unit wrap.
    await admin()
    const unit = document.querySelector('.brand-who-me')
    expect(unit, 'no .brand-who-me unit').not.toBeNull()
    expect(unit!.querySelector('.brand-admin')?.textContent).toBe('admin')
    expect(unit!.textContent).toContain('someone@saga.xyz')
    for (const env of [MID_W, WIDE_W]) {
      const nowrap =
        painted(unit!, 'white-space', env) === 'nowrap' || painted(unit!, 'flex-wrap', env) === 'nowrap'
      expect(nowrap, `the unit wraps at ${env.width}`).toBe(true)
    }
  })

  it('holds one row at 390: the mark, the environment, the tenant key and id, the admin tag', async () => {
    // MUTATION: `flex-wrap: wrap` back on the row, the wordmark drawn, or any
    // of the three hidden facts drawn again -- or the id or the tag hidden.
    await admin()
    const row = document.querySelector('.brand-row')!
    expect(painted(row, 'flex-wrap', PHONE_W)).toBe('nowrap')
    expect(painted(row, ['gap', 'column-gap'], PHONE_W)).toBe('var(--ctl-s2)')
    expect(painted(document.querySelector('.brand-word')!, 'display', PHONE_W)).toBe('none')
    // The link keeps its name from the mark's title.
    expect(document.querySelector('.brand-home svg')?.getAttribute('aria-label')).toBe('SwarmCloud')

    // NOT DRAWN, BUT STILL ANNOUNCED: the display name and the principal.
    for (const sel of ['.brand-who-name', '.brand-who-principal']) {
      const el = document.querySelector(sel)
      expect(el, `${sel} is not rendered at all`).not.toBeNull()
      expect(visuallyHidden(el!, PHONE_W), `${sel} is drawn at 390`).toBe(true)
      expect(notDrawn(el!, WIDE_W), `${sel} is hidden at 1440`).toBe(false)
    }

    // DRAWN: the tenant id and the admin tag. Only the id gives way.
    // RE-POINTED (CH-20's copy): the id is inside its own copy control now,
    // and the control is what gives way in the row; the id ellipsizes in it.
    const copy = document.querySelector('.brand-who > .brand-id')!
    expect(copy, 'the tenant id is not in its copy control').not.toBeNull()
    const id = copy.querySelector(':scope > .id')!
    expect(notDrawn(copy, PHONE_W), 'the tenant id is hidden at 390').toBe(false)
    expect(notDrawn(document.querySelector('.brand-admin')!, PHONE_W), 'the admin tag is hidden at 390').toBe(false)
    expect(painted(copy, 'min-width', PHONE_W)).toBe('0')
    expect(painted(id, 'white-space', PHONE_W)).toBe('nowrap')
    expect(painted(id, 'text-overflow', PHONE_W)).toBe('ellipsis')
    expect(painted(id, ['overflow', 'overflow-x'], PHONE_W)).toBe('hidden')
    expect(painted(id, 'min-width', PHONE_W)).toBe('0')
    expect(copy.getAttribute('title'), 'the cut id keeps its full value').toBe('u-bogdan')
  })

  it('makes the cut tenant id its own copy control: whole in its title and in what it copies', async () => {
    // The decision: the id ellipsizes "with its full value in `title` and in
    // a copy (the AH-11 precedent)". A CSS ellipsis leaves the text intact,
    // but selecting a cut id in a 52px bar on a phone is not a copy anyone
    // can rely on. The control IS the id, so it costs the row no width -- a
    // separate button would take the 60-70px the id keeps at 390.
    // MUTATION: render the id as a plain span again, copy anything but the
    // whole id, or say nothing when the copy lands or fails.
    const writeText = vi.fn(async (_: string) => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      await admin()
      const copy = document.querySelector<HTMLElement>('.brand-who > .brand-id')
      expect(copy?.tagName, 'the tenant id is not a control').toBe('BUTTON')
      expect(copy!.getAttribute('type')).toBe('button')
      expect(copy!.getAttribute('title')).toBe('u-bogdan')
      expect(copy!.getAttribute('aria-label') ?? '', 'the control does not say what it copies').toMatch(
        /copy.*u-bogdan/i,
      )
      await act(async () => {
        fireEvent.click(copy!)
      })
      expect(writeText).toHaveBeenCalledWith('u-bogdan')
      // Said, not silent: the outcome is announced beside the control.
      expect(document.querySelector('.brand-who [role="status"]')?.textContent ?? '').toMatch(/tenant id copied/)
      // §7.2: a 44px target at 560px and below, the type unchanged.
      expect(Number.parseFloat(painted(copy!, 'min-height', PHONE_W) ?? '0')).toBeGreaterThanOrEqual(44)
    } finally {
      delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }
  })

  it('says so when the browser refuses the copy, rather than claiming it', async () => {
    // `navigator.clipboard` is undefined outside a secure context, and a
    // write can be refused. MUTATION: announce "copied" whatever happened.
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn(async () => Promise.reject(new Error('denied'))) },
      configurable: true,
    })
    try {
      await admin()
      await act(async () => {
        fireEvent.click(document.querySelector('.brand-who > .brand-id')!)
      })
      const said = document.querySelector('.brand-who [role="status"]')?.textContent ?? ''
      expect(said).not.toMatch(/tenant id copied/)
      expect(said).toMatch(/refused/)
    } finally {
      delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }
  })

  it('shortens the unknown badge to UNKNOWN at 390 and keeps the full name for a reader', () => {
    // MUTATION: drop the short label, or hide "ENVIRONMENT" with display:none.
    render(<EnvironmentBadge env={{ kind: 'unknown', host: 'swarm.example' }} />)
    const badge = document.querySelector('.brand-env.is-unknown')!
    expect(badge.textContent, 'the accessible name lost the full words').toContain('ENVIRONMENT UNKNOWN')
    const lead = badge.querySelector('.brand-env-lead')
    expect(lead?.textContent?.trim()).toBe('ENVIRONMENT')
    expect(visuallyHidden(lead!, PHONE_W), '"ENVIRONMENT" is still drawn at 390').toBe(true)
    expect(notDrawn(lead!, WIDE_W)).toBe(false)
    // The host stays in the title and in the tree, and is not drawn at 390.
    expect(badge.getAttribute('title') ?? '').toContain('swarm.example')
    const host = badge.querySelector('.id')!
    expect(host.textContent).toBe('swarm.example')
    expect(visuallyHidden(host, PHONE_W), 'the host id is still drawn at 390').toBe(true)
  })

  it('draws the admin marker as a grey hairline tag, not an accent pill', async () => {
    // The same treatment as `.brand-env.is-nonprod`. MUTATION: the --info tint back.
    await admin()
    const tag = document.querySelector('.brand-admin')!
    expect(painted(tag, ['background', 'background-color'], WIDE_W)).toBe('var(--surface-2)')
    expect(painted(tag, 'color', WIDE_W)).toBe('var(--text-dim)')
    expect(painted(tag, ['border', 'border-color'], WIDE_W) ?? '').toMatch(/^1px solid var\(--line\)$/)
  })

  it('leaves nothing at the retired 720px breakpoint', () => {
    // 720 is not one of §7.1's five. MUTATION: the 720 rule back.
    const at720 = flatRules(STYLES).filter(
      (r) => r.conditions.some((c) => /\b720px\b/.test(c)) && /\.brand-/.test(r.selector),
    )
    expect(at720.map((r) => `${r.conditions.join(' ')} ${r.selector}`)).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// B17 -- never restyle an identifier
// ---------------------------------------------------------------------------

describe('B17: an identifier is never restyled', () => {
  it('leaves the workflow id alone inside the section heading', async () => {
    const style = withStyles()
    render(<WorkflowsScreen />)

    // The real screen, the real fixture id, the real stylesheet.
    const id = await screen.findByText('wf_audit_01', {}, { timeout: 4000 })
    const heading = id.closest('h2')
    expect(heading, 'the workflow id is no longer inside the section heading').toBeTruthy()

    expect(getComputedStyle(id).textTransform).toBe('none')
    // The text is the id as the API serves it, in lower case, paste-ready.
    expect(id.textContent).toBe('wf_audit_01')

    // §B4.1 MOVED THE OTHER HALF OF THIS TEST. `.section > h2` used to be
    // `text-transform: uppercase`, and this test read that back to prove it
    // was not passing because the defect had evaporated for some unrelated
    // reason. The type scale fixed the inversion -- a panel title was drawn
    // smaller and fainter than its own rows -- so the heading is now
    // --t-lead in --text (TS-18) and no longer uppercases anything. The precondition
    // therefore moved to a rule that still DOES case-shift, in the next test
    // (`.ctl-chip`, which the restraint pass moved from uppercase to
    // lowercase); it did not get deleted, because a B17 assertion with nothing
    // transforming above it passes on an empty stylesheet.
    expect(getComputedStyle(heading!).textTransform).not.toBe('uppercase')
    style.remove()
  })

  it('beats a case-shifting ANCESTOR by inheritance, not by specificity', () => {
    const style = withStyles()
    // WHAT MOVED, AND WHY THIS IS THE SAME TEST.
    //
    // `.ctl-chip` is still the live case-shifting ancestor B17 was written
    // for; the restraint pass changed the DIRECTION of the shift, not its
    // existence. The chip used to be UPPERCASE -- 600/13px/mono/tracked, the
    // badge treatment -- and design-system.md §6.6 replaced that with Vercel's
    // measured status idiom: a 10px dot and the state word at --t-body in
    // --text, `text-transform: lowercase` so that the API's shouted
    // `RUNNING` / `REAUTH_REQUIRED` render the way `.wf-state` already renders
    // them on Workflows.
    //
    // So the precondition is re-pointed at `lowercase` rather than deleted,
    // and the assertion it protects is UNCHANGED and now harder to satisfy:
    // lowercase is the more dangerous direction for an identifier. An
    // uppercased id still looks like an id someone shouted; a LOWERCASED one
    // silently becomes a different string to anyone who retypes what they see.
    const { container } = render(
      <span className="ctl-chip">
        RUNNING <span className="id">WF_AUDIT_01</span>
      </span>,
    )
    const chip = container.querySelector('.ctl-chip')!
    const id = container.querySelector('.id')!
    // The ancestor really does case-shift, so the assertion below is not
    // passing on a stylesheet that failed to load.
    expect(getComputedStyle(chip).textTransform).toBe('lowercase')
    expect(getComputedStyle(id).textTransform).toBe('none')
    style.remove()
  })

  it('and the rule reaches literals that were never wrapped in anything', () => {
    const style = withStyles()
    const { container } = render(
      <span className="ctl-chip">
        route <code>/v1/tasks</code>
      </span>,
    )
    // `lowercase`, not `uppercase` -- see the note in the test above. The
    // ancestor still transforms; a route literal still must not.
    expect(getComputedStyle(container.querySelector('.ctl-chip')!).textTransform).toBe(
      'lowercase',
    )
    expect(getComputedStyle(container.querySelector('code')!).textTransform).toBe('none')
    style.remove()
  })
})

// ---------------------------------------------------------------------------
// B19 -- the layout must not waste the screen
// ---------------------------------------------------------------------------

describe('B19: the console uses the glass it is given', () => {
  it('is no longer centred in a 1100px lane', () => {
    const style = withStyles()
    const { container } = render(<div className="app" />)
    const app = container.querySelector('.app')!

    // Declared as the token, not as a literal: putting a number back here is
    // what this asserts against.
    expect(getComputedStyle(app).maxWidth).toBe('var(--app-max)')

    const max = getComputedStyle(document.documentElement).getPropertyValue('--app-max').trim()
    expect(max).toMatch(/^\d+px$/)
    // A 1600px display must be usable end to end; the remaining cap exists
    // only so an ultrawide does not produce a table row the eye cannot track.
    expect(Number.parseInt(max, 10)).toBeGreaterThanOrEqual(1600)
    style.remove()
  })

  /**
   * WHAT MOVED: the breakpoints now widen `--ctl-gutter`, not `--app-pad`.
   *
   * §B5.1 of `styles.css` collapsed two ladders into one. The page's edge
   * padding ran 16 → 24 → 32 as `--app-pad`, while the distance between the
   * rail and the content was a flat `--ctl-s5`, and at 1280px those two
   * expressions of the same idea disagreed by 4px. `--ctl-gutter` is now THE
   * distance between two top-level regions — the page's side padding and the
   * rail-to-work gap — and `--app-pad` is a constant alias of it, kept by name
   * because the product header lines its wordmark up with `.app` through it.
   *
   * So the thing that grows with the viewport is `--ctl-gutter`, and that is
   * what this reads. The second half of the test is unchanged and is still the
   * claim that matters: `.brand-row` and `.app` must resolve the SAME token,
   * or the wordmark drifts away from the rail at the first breakpoint.
   */
  it('grows its gutter at wide breakpoints, and the header follows it', () => {
    const style = withStyles()
    const sheet = style.sheet!

    // Via the CSSOM rather than a regex: jsdom parses the rules but cannot
    // evaluate `min-width` without a viewport, so the breakpoints are read
    // rather than exercised. See the note at the top of this file.
    const widened = [...sheet.cssRules]
      .filter((r): r is CSSMediaRule => r.constructor.name === 'CSSMediaRule')
      .filter((r) => /min-width/.test(r.conditionText))
      .filter((r) => [...r.cssRules].some((inner) => inner.cssText.includes('--ctl-gutter')))
      .map((r) => r.conditionText)

    expect(widened.length, 'no wide breakpoint changes the page gutter').toBeGreaterThanOrEqual(2)

    // AND `--app-pad` IS STILL THE PAGE GUTTER rather than a second number
    // that happens to look like it. If somebody re-splits the two ladders,
    // this is what catches it.
    const pad = getComputedStyle(document.documentElement).getPropertyValue('--app-pad').trim()
    expect(pad, '--app-pad no longer derives from --ctl-gutter').toBe('var(--ctl-gutter)')

    // The header sits OUTSIDE `.app` so its environment bar can reach both
    // viewport edges; this is what keeps its wordmark lined up with the
    // content column anyway, at every one of those breakpoints.
    const { container } = render(<div className="brand-row" />)
    const row = getComputedStyle(container.querySelector('.brand-row')!)
    // The shorthand, read whole: jsdom does not expand `padding` into its four
    // longhands when the value is a custom property, so asking for
    // `paddingLeft` would silently answer "" and pass.
    expect(row.padding).toContain('var(--app-pad)')
    expect(row.maxWidth).toBe('var(--app-max)')

    // ALIGNMENT IS THE CLAIM, so both sides of it are asserted: the content
    // column has to be reading the same two tokens, or the wordmark drifts
    // away from the nav at the first breakpoint.
    const app = render(<div className="app" />)
    const appStyle = getComputedStyle(app.container.querySelector('.app')!)
    expect(appStyle.padding).toContain('var(--app-pad)')
    expect(appStyle.maxWidth).toBe('var(--app-max)')
    style.remove()
  })

  it('clamps a sentence even though the page is no longer clamped', () => {
    const style = withStyles()
    const { container } = render(<p className="sub">a line of prose</p>)
    expect(getComputedStyle(container.querySelector('.sub')!).maxWidth).toBe('var(--measure)')
    expect(
      getComputedStyle(document.documentElement).getPropertyValue('--measure').trim(),
    ).toMatch(/ch$/)
    style.remove()
  })
})
