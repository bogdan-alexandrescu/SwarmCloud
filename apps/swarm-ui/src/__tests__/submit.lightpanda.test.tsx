// LP-UI (docs/design/lightpanda-runtime.md §6.3 and §6.6, contract request
// 127): choosing `claude-code-lightpanda` on the Submit form
//
//   * offers the prompt field, as claude-code-large does;
//   * draws no browser-tool picker -- that picker is claude-code-browser's,
//     and this profile declares no `browser_tool`;
//   * shows the profile's one-line hint, in the design's words, so a person
//     who reads "browser" in the name does not expect a live view.
//
// MUTATIONS, each of which turns a case red: drop the profile's SUGGESTED
// entry; declare `browser_tool` for it; drop or reword its PROFILE_HINTS line.
//
// Nothing here is credential-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitScreen } = await import('../Submit')

const WAIT = { timeout: 4000 }
const PROFILE = 'claude-code-lightpanda'
const HINT =
  'reads and fills web pages without a screen — no live view, no screenshots, no iframes; pages it cannot read go to a live-browser child'
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

/** The fixture's capacity with the new profile served, shaped as claude-code's
 *  on the `standard` class, with the input contract api.ts mirrors for it. */
async function capacity(): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  const base = data.runner_profiles['claude-code']!
  data.runner_profiles[PROFILE] = {
    ...base,
    resource_class: 'standard',
    backend: 'GKE_AUTOPILOT',
    available: true,
    disabled_reason: '',
    pools: base.pools.map((p) => (p === 'runner:claude-code' ? `runner:${PROFILE}` : p)),
    input_contract: { required_keys: ['prompt'] },
  }
  return { status: 'ok', data, fetchedAt: Date.now() }
}

async function choose(name: string) {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  const radio = r.container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${name}"]`)
  expect(radio, `${name} is not offered on the runner list`).not.toBeNull()
  fireEvent.click(radio!)
  return r.container
}

beforeEach(async () => {
  api.loadCapacity.mockResolvedValue(await capacity())
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('claude-code-lightpanda on the Submit form', () => {
  it('offers the prompt field', async () => {
    const c = await choose(PROFILE)
    const build = c.querySelector('.sbf-build')!
    expect(build.querySelector('textarea'), 'no prompt field for the profile').not.toBeNull()
    expect(visible(build)).toContain('the whole instruction, passed as one argument')
  })

  it('draws no browser-tool picker', async () => {
    await choose(PROFILE)
    expect(screen.queryByRole('radiogroup', { name: 'Browser tool' })).toBeNull()
  })

  it('shows the profile’s one-line hint, in the design’s words', async () => {
    const c = await choose(PROFILE)
    expect(visible(c.querySelector('.sbf-profile-hint'))).toBe(HINT)
  })

  it('shows no hint for a profile that has none', async () => {
    const c = await choose('claude-code')
    expect(c.querySelector('.sbf-profile-hint')).toBeNull()
  })
})
