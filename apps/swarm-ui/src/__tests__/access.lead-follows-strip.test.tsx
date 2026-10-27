// CONFIG › GITHUB ACCESS: THE LEAD FOLLOWS THE SETUP STRIP (GITHUB-ACCESS-S2B;
// part of #2391, VQ-037 and VQ-038; config-e.html S2b).
//
// WHAT EACH CASE HOLDS:
//   * the lead's step, its state and its one button come from the setup read
//     the strip draws (`SetupContext`, `useSetupSteps`), never from the
//     page's own grant count: with the strip's step 2 done the lead names the
//     strip's next step, on both tabs, and the Choose tab's button is that
//     step's page rather than "Next: Repositories";
//   * while step 2 is the strip's, its "next" or "in progress" is the strip's
//     word, whatever the page counts;
//   * the lead draws one button, the primary one, beside its sentence;
//   * the Connection tab's body is S2b's two columns: the GitHub card (with
//     its quota lines) and Where the App is installed on the left, the rules
//     checklist on the right.
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { painted } from './marks'

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

const GRANT = (n: number) => ({
  repo_id: `repo_${String(n).padStart(16, '0')}`,
  repository: `example-org/example-${n}`,
  owner: 'example-org',
  mode: 'read',
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: { clone: { state: 'ok', code: null, checked_at: null } },
  verified_at: null,
})

const RESET = '2026-10-26T02:13:00+00:00'
const QUOTA = [{ kind: 'connection', name: 'octo-dev', limit: 5000, remaining: 4100, reset_at: RESET, resource: 'core', observed_at: RESET, low: false }]

type StepState = 'done' | 'todo' | 'in_progress'

/** The setup read the strip draws: GitHub and Repositories in the given states, and N secrets needed. */
function setupDoc(repos: StepState, secretsNeeded: number) {
  const step = (name: string, state: StepState, evidence: Record<string, unknown>) => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
  return {
    tenant_id: 'eng',
    derived_at: new Date().toISOString(),
    steps: [
      step('github_connected', 'done', { login: 'octo-dev' }),
      step('repos_chosen', repos, repos === 'done' ? { repositories: ['a', 'b', 'c'] } : {}),
    ],
    config_setup: {
      secrets: {
        needed: secretsNeeded,
        items: Array.from({ length: secretsNeeded }, (_, i) => ({ kind: 'secret', name: `KEY_${i}` })),
        sources: { repositories: { needed: secretsNeeded, reason: null } },
      },
      people: null,
    },
  }
}

async function mount(path: string, grants: unknown[], doc: unknown, personal = true) {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG], grants, github_quota: QUOTA } }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: { tenant_id: 'eng', orgs_listed: true, install_url: null, owners: [{ ...ORG, install_url: null }] } }
    if (m === 'GET' && url.startsWith('/v1/access/orgs/example-org/repositories')) {
      return { status: 200, body: { tenant_id: 'eng', owner: 'example-org', repositories: [], page: 1, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null, total_count: 0 } }
    }
    return null
  })
  window.history.replaceState(null, '', path)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  const { SetupContext } = await import('../ConfigSetup')
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: doc as never, fetchedAt: Date.now() }, personal }}>
      <AccessScreen />
    </SetupContext.Provider>,
  )
  return (await screen.findByRole('region', { name: 'Next step' })) as HTMLElement
}

const buttonsOf = (lead: HTMLElement) => [...lead.querySelectorAll<HTMLElement>('.c-btn')]

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('the lead names the step the strip calls next, not the page’s own count', () => {
  it('Connection: with the strip’s step 2 done, the lead is step 3, Secrets set, with one button to it', async () => {
    // The strip: 2 of 3 done, Secrets set next. The page counts 3 grants.
    const lead = await mount('/config/github', [GRANT(1), GRANT(2), GRANT(3)], setupDoc('done', 2))
    const words = visible(lead)
    // MUTATION: build the kicker from `overview.grants.length` again ("Step 2 of 3 · in progress.").
    expect(words).toContain('Step 3 of 3 · next.')
    expect(words).not.toContain('Step 2')
    expect(words).not.toContain('in progress')
    // Titled by the step's action, not by the count (S2b).
    expect(within(lead).getByRole('heading', { level: 2 }).textContent).toBe('Set the secrets your repositories need')
    expect(words).not.toContain('3 repositories chosen ·')
    // ONE BUTTON, THE PRIMARY ONE. MUTATION: draw "Repositories" beside "Choose repositories" again.
    const buttons = buttonsOf(lead)
    expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Next: Secrets'])
    expect(buttons[0]!.classList.contains('is-primary')).toBe(true)
    expect(buttons[0]!.getAttribute('href')).toBe('/config/secrets')
  })

  it('Choose: with the strip’s step 2 done, the button is the strip’s next step, not "Next: Repositories"', async () => {
    const lead = await mount('/config/github?view=choose', [GRANT(1), GRANT(2), GRANT(3)], setupDoc('done', 2))
    // MUTATION: keep the Choose tab's hard-coded "Next: Repositories".
    expect(buttonsOf(lead).map((b) => b.textContent?.trim())).toEqual(['Next: Secrets'])
    expect(visible(lead)).toContain('Step 3 of 3 · next.')
    expect(visible(lead)).not.toContain('in progress')
  })

  it('in a group workspace the next step after Secrets set is People invited, numbered of four', async () => {
    const doc = { ...setupDoc('done', 0), config_setup: { secrets: { needed: 0, items: [], sources: {} }, people: { applies: true, done: false, others: 0, reason: null } } }
    const lead = await mount('/config/github', [GRANT(1)], doc, false)
    expect(visible(lead)).toContain('Step 4 of 4 · next.')
    expect(buttonsOf(lead).map((b) => b.textContent?.trim())).toEqual(['Next: People and roles'])
  })

  it('while step 2 is the strip’s next, its word is the strip’s state, whatever the page counts', async () => {
    // The strip says in progress; the page counts nothing (its own read lags).
    let lead = await mount('/config/github', [], setupDoc('in_progress', 0))
    // MUTATION: `chosen === 0 ? 'next' : 'in progress'`.
    expect(visible(lead)).toContain('Step 2 of 3 · in progress.')
    expect(buttonsOf(lead).map((b) => b.textContent?.trim())).toEqual(['Choose repositories'])
    document.body.innerHTML = ''
    // The strip says not yet; the page counts two.
    lead = await mount('/config/github', [GRANT(1), GRANT(2)], setupDoc('todo', 0))
    expect(visible(lead)).toContain('Step 2 of 3 · next.')
    expect(within(lead).getByRole('heading', { level: 2 }).textContent).toBe('Choose the repositories SwarmCloud may reach')
  })

  it('with every step done, the lead says setup is complete and offers Choose repositories alone', async () => {
    const lead = await mount('/config/github', [GRANT(1)], setupDoc('done', 0))
    expect(visible(lead)).toContain('Setup complete · 3 of 3.')
    expect(visible(lead)).not.toContain('next')
    expect(buttonsOf(lead).map((b) => b.textContent?.trim())).toEqual(['Choose repositories'])
  })
})

describe('the Connection tab is S2b’s two columns', () => {
  it('puts the GitHub card and Where the App is installed on the left, the rules on the right, quota inside the GitHub card', async () => {
    await mount('/config/github', [GRANT(1)], setupDoc('done', 1))
    await screen.findByRole('table', { name: 'Where the App is installed' })
    const body = document.querySelector<HTMLElement>('.ac-body')
    // MUTATION: mount the cards one under another again, outside `.ac-body`.
    expect(body, 'the two-column body').not.toBeNull()
    const [main, side] = [...body!.children] as HTMLElement[]
    expect(main!.classList.contains('ac-main')).toBe(true)
    expect(main!.querySelector('.ac-conn')).not.toBeNull()
    expect(main!.querySelector('.ac-installs')).not.toBeNull()
    expect(side!.classList.contains('ac-policy')).toBe(true)
    // MUTATION: mount <GitHubQuotaLines> between the install card and the rules again.
    const quota = screen.getByRole('list', { name: 'GitHub quota' })
    expect(quota.closest('.ac-conn'), 'the quota lines sit in the GitHub card').not.toBeNull()
    // THE MARK KEEPS ITS WIDTH (render, phone-390): shrunk to 6px, the warn
    // triangle overflowed its gap and touched the words. MUTATION: drop `flex: none`.
    const mark = quota.querySelector<HTMLElement>('li > [data-mark]')!
    expect(painted(mark, 'flex', { width: 390 })).toBe('none')
    // 2:1 at 1440, one column at 390. MUTATION: drop the 899px one-column rule.
    expect(painted(body!, 'grid-template-columns', { width: 1440 })).toBe('minmax(0, 2fr) minmax(0, 1fr)')
    expect(painted(body!, 'grid-template-columns', { width: 390 })).toBe('minmax(0, 1fr)')
    // The rules are a checklist of three short lines, each headline with its line (S2b).
    const rules = [...side!.querySelectorAll<HTMLElement>('[data-rule]')]
    expect(rules.map((r) => r.querySelector('.ac-rule-t > b')?.textContent)).toEqual([
      'Read means read, and SwarmCloud enforces it.',
      'No workflow changes.',
      'Verifying only reads.',
    ])
    expect(rules.map((r) => r.querySelector('.ac-rule-t > small')?.textContent)).toEqual([
      'A read grant never pushes, whatever GitHub would allow.',
      'SwarmCloud never asks for the workflows permission.',
      'Push test is opt-in, per repository.',
    ])
  })
})
