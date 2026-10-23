// SETUP AND ACCESS, MEASURED THE WAY THE SPACING SWEEP MEASURES THEM
// (SETUP-ACCESS-STYLES; part of #1680).
//
// QAFIX6-FIXTURE (PR 2086) found that serving Work › Setup and Work › Access
// in the fixture build makes the 'setup' slice of the spacing sweep report
// three findings:
//
//   1. the next step's small ghost Re-check, on `.ob-step.is-next`'s
//      `--sk-acs` ground: a control boundary of 2.89:1 dark, 2.96:1 light;
//   2. `ol.ob-list`: 6 stacked surfaces with a 0px gutter;
//   3. `ul.ac-grid`: 2 stacked surfaces with a 0px gutter (dark).
//
// This file holds each one without waiting for the fixture to serve the
// pages: the checklist with one step left to do, and the Access grants grid.
// It measures with the sweep's own `probe`, over the sheet the sweep resolves
// (every `src/styles/*.css`, then `styles.css`, each `var()` substituted per
// theme), so a pass here is the arithmetic `spacing.setup.test.tsx` runs.
//
// Fixtures only; no network. Nothing here is token-shaped.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { Checklist } from '../Onboarding'
import type { OnboardingDoc } from '../types'
import { serve } from './repofixture'
import { FLOORS, probe, resolveSheet, type Finding } from './spaceprobe'

type Theme = 'dark' | 'light'

// The sweep's sheet in the sweep's order (spacingsweep.tsx `SECTION_SHEETS`,
// then `STYLES`): the section sheets load first because main.tsx imports App,
// and so every screen's sheet, before styles.css.
const SECTION_SHEETS = Object.values(
  import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true }),
)
const SHEET = [...SECTION_SHEETS, STYLES].join('\n')
const RESOLVED: Record<Theme, string> = { dark: resolveSheet(SHEET, 'dark'), light: resolveSheet(SHEET, 'light') }
const THEMES: readonly Theme[] = ['dark', 'light']

/** Probe `root` under one theme's resolved sheet, as the sweep's dark and light passes do. */
function measure(root: HTMLElement, theme: Theme): Finding[] {
  const style = document.createElement('style')
  style.textContent = RESOLVED[theme]
  document.head.appendChild(style)
  try {
    return probe(root).findings
  } finally {
    style.remove()
  }
}

const said = (fs: Finding[]) => fs.map((f) => `${f.kind} ${f.where} [${f.side}] ${f.measured} < ${f.floor} — ${f.detail}`)

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

// ---------------------------------------------------------------------------
// The checklist, one step left to do
// ---------------------------------------------------------------------------

function step(name: string, state: string, evidence: Record<string, unknown> = {}) {
  return { step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] }
}

// Every step done but verifying access, which is under way: it is `next_step`,
// so its row is `.ob-step.is-next`, and an in-progress Access step draws its
// "Verify access" link beside a small ghost "Re-check" -- the button the sweep
// measured. `ready` waits on it, as the server derives it. Seven steps, so six
// rows carry the divider that makes each a surface to the probe: the sweep's
// "6 stacked surfaces".
const ONE_LEFT = {
  tenant_id: 'eng',
  user: 'dev@swarm.example.com',
  user_hash: '89abcdef01234567',
  derived_at: new Date().toISOString(),
  next_step: 'access_verified',
  complete: false,
  source: 'derived on this read',
  steps: [
    step('signed_in', 'done', { email: 'dev@swarm.example.com', tenant_id: 'eng' }),
    step('github_connected', 'done', { via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'active' }),
    step('app_installed', 'done', { needed: true, installed: ['example-org'] }),
    step('orgs_enabled', 'done', {}),
    step('repos_chosen', 'done', {}),
    step('access_verified', 'in_progress', {}),
    step('ready', 'todo', { waiting_for: 'access_verified' }),
  ],
} as unknown as OnboardingDoc

// INSIDE `.app`, WHERE THE CONSOLE DRAWS IT. styles.css's zero-specificity
// `:where(.app button)` floor only reaches a button inside `.app`, and that
// rule is what the sweep's 2.89:1 border comes from (item 1 below).
function renderChecklist(): HTMLElement {
  const { container } = render(
    <div className="app">
      <Checklist doc={ONE_LEFT} reload={() => {}} />
    </div>,
  )
  return container
}

describe('the checklist with one step left to do', () => {
  it('renders the next step with its small ghost Re-check, among seven stacked steps', () => {
    const root = renderChecklist()
    const next = root.querySelector('li.ob-step.is-next')
    expect(next?.getAttribute('data-step')).toBe('access_verified')
    expect(next?.querySelector('button.c-btn.is-ghost.is-sm')?.textContent).toBe('Re-check')
    expect(root.querySelectorAll('ol.ob-list > li.ob-step')).toHaveLength(7)
  })

  for (const theme of THEMES) {
    // HELD RED BY A FILE OUTSIDE THIS LANE, so `it.fails`: it passes while the
    // finding stands and fails the day it is gone, which is when it must be
    // turned into `it`. jsdom resolves the cascade by source order alone (no
    // specificity, no `!important`), and every section sheet loads BEFORE
    // styles.css, so `:where(.app button) { border: 1px solid var(--ctl-bd) }`
    // out-ranks `.c-btn.is-ghost { border-color: transparent }` here, though
    // no browser draws that border. No rule in styles/onboarding.css can win
    // that tie. The fix is a restatement in styles.css after the `:where`
    // floor, as styles.css already does for capacity.css's borderless buttons
    // ("BORDERLESS BUTTONS IN A SECTION SHEET"); the lane asked the owner for
    // it (questions.json, SETUP-ACCESS-STYLES).
    it.fails(`draws no control boundary under 3:1 on the next step's ghost button (${theme})`, () => {
      const root = renderChecklist()
      const findings = measure(root, theme).filter(
        (f) => f.kind === 'divider-under-floor' && f.where === 'button.c-btn.is-ghost.is-sm',
      )
      // MUTATION: this reads `--ctl-bd` on `--sk-acs`, 2.89:1 dark and 2.96:1
      // light, and fails -- which `it.fails` reports as a pass until
      // styles.css restates the ghost's own border after `:where(.app button)`.
      expect(said(findings)).toEqual([])
    })
  }

  for (const theme of THEMES) {
    it(`holds the stacked steps apart by at least the sweep's gutter (${theme})`, () => {
      const root = renderChecklist()
      const findings = measure(root, theme).filter((f) => f.kind === 'surfaces-touch' && f.where === 'ol.ob-list')
      // MUTATION: remove `gap` from `.ob-list` in styles/onboarding.css and
      // this reports "6 stacked surfaces with a 0px gutter" and fails; set it
      // under the sweep's floor (`FLOORS.boxGutter`, 4px) and it fails too.
      expect(said(findings)).toEqual([])
      expect(FLOORS.boxGutter).toBe(4)
    })
  }
})

// ---------------------------------------------------------------------------
// Access › the grants grid
// ---------------------------------------------------------------------------

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`

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

const GRANT = (n: number, repository: string, mode: 'read' | 'write') => ({
  repo_id: ID(n),
  repository,
  owner: 'example-org',
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: {
    clone: { state: 'ok', code: null, checked_at: null },
    push: { state: mode === 'write' ? 'ok' : 'not_required', code: null, checked_at: null },
    pull_request: { state: mode === 'write' ? 'ok' : 'not_required', code: null, checked_at: null },
  },
  verified_at: null,
})

// THREE GRANTS FOR THE SWEEP'S "2 STACKED SURFACES". The first row of the grid
// draws no top rule (`.ac-grant:first-child`), so it is not a surface to the
// probe; two cards with a rule each take three rows, as the fixture serves.
const OVERVIEW = {
  tenant_id: 'eng',
  connection: {
    connection_id: 'github:eng:abc',
    forge: 'github',
    method: 'app_user',
    forge_login: 'octo-dev',
    forge_user_id: 42,
    state: 'active',
    failure: null,
    access_expires_at: null,
    refresh_expires_at: null,
    refreshed_at: null,
    refreshing: false,
    created_at: new Date().toISOString(),
    connected_at: new Date().toISOString(),
    revoked_at: null,
  },
  orgs: [ORG],
  grants: [
    GRANT(1, 'example-org/example-docs', 'read'),
    GRANT(2, 'example-org/example-service', 'write'),
    GRANT(3, 'example-org/example-web', 'write'),
  ],
}

async function renderGrants(): Promise<HTMLElement> {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: OVERVIEW }
    if (m === 'GET' && url === '/v1/access/orgs')
      return { status: 200, body: { tenant_id: 'eng', orgs_listed: true, install_url: null, owners: [{ ...ORG, install_url: null }] } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  const { container } = render(<AccessScreen />)
  await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
  return container
}

describe('the Access grants grid', () => {
  for (const theme of THEMES) {
    it(`holds the stacked grant cards apart by at least the sweep's gutter (${theme})`, async () => {
      const root = await renderGrants()
      const grid = root.querySelector('ul.ac-grid')!
      expect(grid.querySelectorAll(':scope > li.ac-grant')).toHaveLength(3)
      const findings = measure(root, theme).filter((f) => f.kind === 'surfaces-touch' && f.where === 'ul.ac-grid')
      // MUTATION: remove `gap` from `.ac-grid` in styles/onboarding.css and
      // this reports "2 stacked surfaces with a 0px gutter" and fails.
      expect(said(findings)).toEqual([])
    })
  }
})
