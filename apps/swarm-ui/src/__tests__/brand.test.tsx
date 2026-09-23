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
import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

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
import { Screen } from '../Shell'
import { WorkflowsScreen } from '../Workflows'
import type { Me } from '../types'

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
    expect(document.querySelector('.brand-env-name')?.textContent).toBe('DEV')
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
    expect(who.textContent).toContain('Your session expired')
    expect(who.textContent).toContain('unread')
    // And no invented tenant anywhere in the bar.
    expect(document.querySelector('.brand-who .id')).toBeNull()
  })

  it('says it is still reading rather than showing an empty slot', () => {
    render(<ProductHeader load={() => new Promise(() => {})} declaredEnv="dev" host="localhost" />)
    expect(document.querySelector('.brand-who.is-pending')?.textContent).toContain('Reading who you are')
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
    // --t-title in --text and no longer uppercases anything. The precondition
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
