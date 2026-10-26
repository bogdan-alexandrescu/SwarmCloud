// THE CONFIG LEAD'S TINT, AND ITS BUTTONS' BORDERS ON IT (CONFIG-LEAD-TINT;
// part of #2157, proposal E, docs/web-ui/mockups/config-e.html `.lead`).
//
// Every Config page opens with a lead: one next-step sentence and a button.
// Proposal E draws it on the info tint. GITHUB-ACCESS-PAGE drew it on the
// plain card surface instead, because a secondary button's `--ctl-bd` border
// measured 2.89:1 on that tint in dark (and 2.96:1 in light), under the 3:1 a
// control boundary is held to (WCAG 2.1 SC 1.4.11; `FLOORS.dividerContrast`
// in spaceprobe.ts, the floor the spacing sweep holds every control to). BGD,
// 2026-10-24: keep the tint, and give the lead's buttons a border that clears
// 3:1 on it in both themes.
//
// WHY A FILE OF ITS OWN RATHER THAN THE SWEEP. The sweep renders each route
// once, in whatever state the fixtures leave it, and a lead's buttons depend on
// the state: "Connect GitHub" alone with nothing connected, "Choose
// repositories" once connected, "Next: Repositories" on the Choose tab. The
// 2.89:1 border was the SECONDARY one ("Repositories" beside "Choose
// repositories", until GITHUB-ACCESS-S2B gave S2b's lead one primary button),
// so this renders each lead state on purpose, through the same resolved sheet
// and the same probe the sweep uses.
//
// WHAT IT HOLDS, in both themes:
//   * the lead is tinted: its card paints `--sk-acs`, not `--surface` (a lead
//     drawn on the plain card again fails here, not by eye);
//   * every button in it clears 3:1 against what is painted behind it, with
//     the numbers printed;
//   * the probe finds no divider under its floor anywhere in the lead.
//
// WHERE THE RULES LIVE. The tint is config.css's `.ac-lead > .c-card`; the
// buttons' borders are in styles.css after the `:where(.app button)` floor,
// because jsdom resolves the cascade by source order and every section sheet
// loads before styles.css (the `.ob-step.is-next` precedent there).
//
// Fixtures only; no network.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { serve } from './repofixture'
import { colour, contrast, over, probe, resolveSheet, tokenTables, FLOORS, type RGBA } from './spaceprobe'

// Every shipped sheet, in the order the app loads them (spacingsweep.tsx).
const SECTION_SHEETS = Object.values(
  import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true }),
)
const SHEET = [...SECTION_SHEETS, STYLES].join('\n')

type Theme = 'dark' | 'light'
const THEMES: readonly Theme[] = ['dark', 'light']

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  state: 'active',
  failure: null,
  access_expires_at: new Date(Date.now() + 7 * 3600_000).toISOString(),
  refresh_expires_at: null,
  refreshed_at: new Date(Date.now() - 600_000).toISOString(),
  refreshing: false,
  created_at: new Date().toISOString(),
  connected_at: new Date().toISOString(),
  revoked_at: null,
}

const ORG = {
  owner: 'example-org',
  owner_type: 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  checked_at: null,
}

const GRANT = {
  repo_id: `repo_${'1'.padStart(16, '0')}`,
  repository: 'example-org/example-docs',
  owner: 'example-org',
  mode: 'read',
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: { clone: { state: 'ok', code: null, checked_at: null } },
  verified_at: null,
}

/** Each lead state, by the buttons it draws. */
const STATES: readonly { name: string; access: Record<string, unknown>; path: string; buttons: string[] }[] = [
  { name: 'nothing connected', access: { connection: null, grants: [] }, path: '/config/github', buttons: ['Connect GitHub'] },
  { name: 'connected, nothing chosen', access: { connection: CONNECTION, grants: [] }, path: '/config/github', buttons: ['Choose repositories'] },
  // THE 2.89:1 WAS MEASURED ON A SECONDARY BUTTON BESIDE THE PRIMARY here.
  // Since GITHUB-ACCESS-S2B (VQ-038) S2b's lead draws its one primary button,
  // with the setup read unmeasured (no SetupContext) as here as well.
  { name: 'connected, one chosen', access: { connection: CONNECTION, grants: [GRANT] }, path: '/config/github', buttons: ['Choose repositories'] },
  { name: 'Choose tab, not connected', access: { connection: null, grants: [] }, path: '/config/github?view=choose', buttons: ['Open Connection'] },
  { name: 'Choose tab, connected', access: { connection: CONNECTION, grants: [GRANT] }, path: '/config/github?view=choose', buttons: ['Next: Repositories'] },
]

async function mount(state: (typeof STATES)[number]) {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: { tenant_id: 'eng', orgs: [ORG], ...state.access } }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: { tenant_id: 'eng', orgs_listed: true, install_url: null, owners: [{ ...ORG, install_url: null }] } }
    if (m === 'GET' && url.startsWith('/v1/access/orgs/example-org/repositories')) {
      return { status: 200, body: { tenant_id: 'eng', owner: 'example-org', repositories: [], page: 1, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null, total_count: 0 } }
    }
    return null
  })
  window.history.replaceState(null, '', state.path)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  // INSIDE `.app`, as the console mounts it: the `:where(.app button)` floor
  // then matches, and jsdom's source-order cascade lets it out-rank any
  // section sheet -- the tie the sweep meets, so a border rule moved back
  // into config.css fails here as it would there.
  render(
    <div className="app">
      <AccessScreen />
    </div>,
  )
  return (await screen.findByRole('region', { name: 'Next step' })) as HTMLElement
}

/** The colour painted behind `el`: its ancestors' backgrounds composited (spaceprobe's `behind`). */
function ground(el: Element): RGBA {
  let acc: RGBA = { r: 0, g: 0, b: 0, a: 0 }
  for (let n = el.parentElement; n !== null; n = n.parentElement) {
    const c = colour(getComputedStyle(n).backgroundColor)
    if (c === null) throw new Error(`unresolved background on ${n.className}`)
    acc = over(acc, c)
    if (acc.a >= 0.999) return acc
  }
  throw new Error('nothing opaque behind the lead')
}

const rgb = (c: RGBA) => `rgb(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)})`

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
  document.querySelectorAll('style[data-lead-sheet]').forEach((s) => s.remove())
})

describe('the Config lead keeps the info tint, and its buttons clear 3:1 on it', () => {
  for (const theme of THEMES) {
    for (const state of STATES) {
      it(`${theme}: ${state.name}`, async () => {
        const lead = await mount(state)
        // The app's surroundings: the lead sits in the page on `--bg`.
        document.body.style.background = 'var(--bg)'
        const style = document.createElement('style')
        style.dataset.leadSheet = theme
        style.textContent = resolveSheet(`${SHEET}\nbody { background: var(--bg); }`, theme)
        document.head.appendChild(style)

        // THE TINT. MUTATION: drop `.ac-lead > .c-card`'s background from config.css.
        const tint = colour(tokenTables(SHEET)[theme].get('--sk-acs') ?? '')
        const card = lead.querySelector('.c-card') as HTMLElement
        expect(card, 'the lead draws its card').not.toBeNull()
        expect(rgb(colour(getComputedStyle(card).backgroundColor)!), 'the lead is drawn on the info tint').toBe(rgb(tint!))

        // EVERY BUTTON, MEASURED. MUTATION: drop styles.css's `.ac-lead .c-btn` border rules (2.89:1 dark, 2.96:1 light).
        const buttons = [...lead.querySelectorAll('.c-btn')] as HTMLElement[]
        expect(buttons.map((b) => b.textContent?.trim())).toEqual(state.buttons)
        const measured = buttons.map((b) => {
          const border = colour(getComputedStyle(b).borderTopColor)!
          const bg = ground(b)
          return { button: b.textContent?.trim(), ratio: contrast(over(border, bg), bg), border: rgb(border), on: rgb(bg) }
        })
        console.log(`${theme} ${state.name}: ${JSON.stringify(measured)}`)
        for (const m of measured) expect(m.ratio, `${m.button}: ${m.border} on ${m.on}`).toBeGreaterThanOrEqual(FLOORS.dividerContrast)

        // AND THE SWEEP'S OWN PROBE, over the whole lead.
        const report = probe(lead)
        expect(report.findings.filter((f) => f.kind === 'divider-under-floor').map((f) => `${f.where} [${f.side}] ${f.measured} — ${f.detail}`)).toEqual([])
        expect(report.unresolved).toEqual([])
      })
    }
  }
})
