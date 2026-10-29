/**
 * THE STRIP AND THE CHECKLIST ON ONE SCREEN (QA1-FIX-10, visual QA pass 1
 * config-08 and chrome-C20; part of #2596).
 *
 * The fixture console drew the strip and the panel's meter "2 of 3 done", and
 * Workspace setup's checklist "4 of 7 done" with Enable orgs failed, on the
 * same page. The strip's GitHub connected read `github_connected` alone, so a
 * failed Enable orgs showed nowhere on it, and its link to the checklist did
 * not say the checklist counted something else.
 *
 *   * a failed or stale checklist step done on GitHub access (Install the
 *     App, Enable orgs) holds strip step 1, its badge and the meter at that
 *     state, named in the checklist's words;
 *   * the strip's link to Workspace setup carries the checklist's own count,
 *     so the two counts are each named;
 *   * GitHub connected's evidence names the login as `forge_login`, the key
 *     onboarding.py serves it under.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { SetupContext, SetupStrip, checklistCount, configBadges, setupSteps } from '../ConfigSetup'
import type { OnboardingDoc } from '../types'

type Json = Record<string, unknown>

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })

/** The fixture console's onboarding read (api.ts `fixtureOnboarding`): seven steps, Enable orgs failed, no `config_setup`. */
const doc = (orgs = 'failed', app = 'done'): OnboardingDoc =>
  ({
    tenant_id: 'eng',
    user: 'alice@example.com',
    user_hash: 'h',
    derived_at: '2026-10-28T10:00:00Z',
    steps: [
      step('signed_in', 'done'),
      step('github_connected', 'done', { via: 'user', kind: 'app_user', forge_login: 'alice-example', token_state: 'active' }),
      step('app_installed', app),
      step('orgs_enabled', orgs),
      step('repos_chosen', 'done', { repositories: [{ repository: 'example-org/example-api' }] }),
      step('access_verified', 'in_progress'),
      step('ready', 'todo'),
    ],
    next_step: 'orgs_enabled',
    complete: false,
    source: 'derived',
  }) as unknown as OnboardingDoc

afterEach(cleanup)

describe('a failed checklist step on GitHub access holds the strip’s step 1', () => {
  it('Enable orgs failed: GitHub connected is failed, named, and the strip calls it next', () => {
    // MUTATION: drop `heldBy(...)` around the github step in setupSteps -> state 'done', line '@alice-example'.
    const github = setupSteps(doc(), true).find((s) => s.key === 'github')!
    expect(github.state).toBe('failed')
    expect(github.line).toBe('Enable orgs failed')
  })

  it('a stale Install the App holds it at "needs a re-check"; failed wins over stale', () => {
    // MUTATION: in heldBy, search `side` for any non-done state in order -> Install the App's stale wins over Enable orgs' failure.
    expect(setupSteps(doc('done', 'stale'), true)[0]).toMatchObject({ state: 'stale', line: 'Install the App needs a re-check' })
    expect(setupSteps(doc('failed', 'stale'), true)[0]).toMatchObject({ state: 'failed', line: 'Enable orgs failed' })
  })

  it('a step still to do or in progress on the checklist does not hold it', () => {
    // MUTATION: let heldBy hold on 'todo' -> GitHub connected reads "Enable orgs not yet" over a connected account.
    expect(setupSteps(doc('todo', 'in_progress'), true)[0]).toMatchObject({ state: 'done', line: '@alice-example' })
  })

  it('the panel’s GitHub access badge says failed, not ✓', () => {
    // MUTATION: as the first test -> the badge is { tone: 'done', text: '✓' }.
    expect(configBadges(doc(), true).github).toEqual({ tone: 'missing', text: 'failed', why: 'GitHub connected: Enable orgs failed' })
  })
})

describe('the strip names the checklist’s count beside its own', () => {
  const strip = (d: OnboardingDoc) =>
    render(
      <SetupContext.Provider value={{ doc: { status: 'ok', data: d, fetchedAt: 0 }, personal: true }}>
        <SetupStrip go={() => undefined} />
      </SetupContext.Provider>,
    )

  it('checklistCount is the checklist’s own words', () => {
    // MUTATION: return null from checklistCount -> the link reads plain "Workspace setup".
    expect(checklistCount(doc())).toBe('Setup checklist: 4 of 7 done')
    expect(checklistCount(null)).toBeNull()
  })

  it('the strip draws its count, the failed step and the checklist’s count on one line of links', () => {
    strip(doc())
    const section = screen.getByRole('region', { name: 'Workspace setup' })
    // Secrets set is unknown with no `config_setup`, so one of three is done: GitHub is held, Repositories done.
    expect(within(section).getByText('1 of 3 steps done')).toBeTruthy()
    const steps = within(section).getByRole('list', { name: 'Workspace setup steps' })
    expect(steps.querySelector('.cfg-step.is-failed')?.textContent).toContain('Next: Enable orgs failed')
    const link = within(section).getByRole('link', { name: 'Setup checklist: 4 of 7 done' })
    expect(link.getAttribute('href')).toBe('/config/workspace/setup')
  })
})
