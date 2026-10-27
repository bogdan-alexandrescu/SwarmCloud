// LIVE QA OF WORK › SETUP, 2026-10-11 (section work-repos-setup-access).
//
// WHAT EACH CASE HOLDS:
//   * RSA-25: a step's title is its words alone -- the API's step id is not
//     copy, and stays only on `data-step`; a step with nothing on its line
//     (the workspace step draws its own detail) says when it was checked
//     without a separator before it;
//   * RSA-26: the summary, which names the optional steps, may wrap
//     rather than being cut on a phone.
//
// Fixtures only; nothing token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { cascade } from './cssgate'
import { ALL_CSS } from './sheets'

const CHECKED = new Date(Date.now() - 3 * 3600_000).toISOString()

function step(name: string, over: Record<string, unknown> = {}) {
  return { step: name, state: 'done', code: null, copy: null, checked_at: CHECKED, evidence: {}, issues: [], ...over }
}

const DOC = {
  tenant_id: 'eng',
  user: 'dev@swarm.example.com',
  user_hash: '89abcdef01234567',
  derived_at: new Date().toISOString(),
  next_step: null,
  complete: true,
  source: 'derived on this read',
  steps: [
    step('signed_in', { evidence: { email: 'dev@swarm.example.com', tenant_id: 'eng' } }),
    step('workspace', { state: 'in_progress', required: false }),
    step('ready', { evidence: { first_repository: 'octo-dev/example-api' } }),
  ],
}

afterEach(() => {
  vi.unstubAllEnvs()
})

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  serve((m, url) => (m === 'GET' && url === '/v1/onboarding' ? { status: 200, body: DOC } : null))
  const { OnboardingScreen } = await import('../Onboarding')
  render(<OnboardingScreen />)
  await screen.findByRole('list', { name: 'Setup steps' })
}

const stepEl = (name: string) => document.querySelector<HTMLElement>(`li.ob-step[data-step="${name}"]`)!

describe('RSA-25: a step row is written for a person', () => {
  it('names no raw step id in its visible text', async () => {
    await mount()
    // The ids that are not also words: their snake case is the API's, never copy.
    expect(visible(stepEl('signed_in').querySelector('.ob-step-h'))).not.toContain('signed_in')
    for (const li of document.querySelectorAll('li.ob-step')) expect(visible(li.querySelector('.ob-step-h'))).not.toMatch(/[a-z]_[a-z]/)
    // The id stays where code reads it.
    expect([...document.querySelectorAll<HTMLElement>('li.ob-step')].map((li) => li.dataset.step)).toEqual(['signed_in', 'workspace', 'ready'])
  })

  it('a step with an empty line starts at "checked", never at a separator', async () => {
    await mount()
    const line = visible(stepEl('workspace').querySelector('.ob-line'))
    expect(line).toMatch(/^checked /)
    // A step with a line keeps the separator between the line and the time.
    expect(visible(stepEl('signed_in').querySelector('.ob-line'))).toMatch(/workspace eng · checked /)
  })
})

describe('RSA-26: the summary wraps instead of being cut', () => {
  it('the count, at a phone width, may wrap', async () => {
    await mount()
    // VQ-048 moved it from a chip in the card's head to a line of its body.
    const summary = document.querySelector('.ob-card > .ob-summary')!
    expect(visible(summary)).toContain('optional')
    // MUTATION: give `.ob-summary` `white-space: nowrap` -> fails.
    expect(cascade(ALL_CSS, summary, 'white-space', { width: 400 }).winner?.value ?? 'normal').toBe('normal')
  })
})
