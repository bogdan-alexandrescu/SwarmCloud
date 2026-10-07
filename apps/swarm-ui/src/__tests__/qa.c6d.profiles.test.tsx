// QA G5 (2026-10-07), By runner profile: two findings in the opened row.
//
//   G5-04  A profile lists a pool the complete pool read does not hold. The
//          matrix cell says `uncapped` -- admission skips a pool with no
//          document (`evaluate_capacity`, "unlimited by construction") -- but
//          the opened row drew `—` twice, the console's word for "not
//          measured". It now says `no pool · uncapped`, and keeps `—` for a
//          pool that really was not read.
//   G5-18  The disabled sentence prefixed `<name> is disabled:` to a server
//          reason that already began that way, and appended `.` to one that
//          already ended in one.
//
// BREAK IT: draw `row === null ? '—'` again in FitsTable -- the first case
// fails. Prefix the name unconditionally in `disabledSentence` -- the second
// fails on "codex is disabled: codex is disabled".

import { fireEvent, render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, ProfileAdmission, RunnerProfile } from '../types'

const loadCapacity = vi.hoisted(() => vi.fn<() => Promise<Result<Capacity>>>())
vi.mock('../api', () => ({ loadCapacity }))

const { ProfilesScreen } = await import('../Profiles')
const { disabledSentence } = await import('../ProfileMatrix')

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-07T10:00:00Z',
  }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 5, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile>): RunnerProfile {
  return { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'git', units: 1, pools: [], ...over }
}

const PHANTOM = 'provider:git:tenant:eng'
const UNREAD = 'provider:git'

function capacity(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-07T10:00:00Z',
    pools_complete: true,
    pools: [pool('global', 1, 40), pool('tenant:eng', 1, 20)],
    runner_profiles: {
      merge: profile({
        pools: ['global', 'tenant:eng', PHANTOM],
        admission: admission({ binding: ['tenant:eng'], uncapped: [PHANTOM] }),
      }),
      // The control: a pool the server could NOT read keeps the dash.
      probe: profile({
        pools: ['global', UNREAD],
        admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: [UNREAD] }),
      }),
      codex: profile({
        provider: 'openai',
        available: false,
        disabled_reason: 'codex is disabled on this platform. Use claude-code.',
        pools: ['global'],
        admission: admission({}),
      }),
    },
  }
}

async function opened(name: string): Promise<HTMLElement> {
  loadCapacity.mockResolvedValue({ status: 'ok', data: capacity(), fetchedAt: Date.now(), serverAt: '2026-10-07T10:00:00Z' })
  render(<ProfilesScreen />)
  await waitFor(() => expect(document.querySelector(`.cap-mx tr[data-profile="${name}"]`)).not.toBeNull())
  fireEvent.click(document.querySelector<HTMLButtonElement>(`.cap-mx tr[data-profile="${name}"] button`)!)
  const exp = document.querySelector<HTMLElement>('.cap-mx tr.cap-mx-exp')
  expect(exp, `${name} did not open`).not.toBeNull()
  return exp!
}

describe('G5-04: a pool with no document reads as uncapped in the opened row, as in the matrix', () => {
  it('says "no pool · uncapped" for it, never the not-measured dash', async () => {
    const exp = await opened('merge')
    const row = exp.querySelector(`tr[data-pool="${PHANTOM}"]`)!
    expect(row, 'the phantom pool has no row').not.toBeNull()
    expect(row.textContent).toContain('no pool · uncapped')
    expect(row.textContent).not.toContain('—')
    // The matrix says the same word for the same pool.
    const cell = document.querySelector(`.cap-mx tr[data-profile="merge"] td[data-pool="${PHANTOM}"]`)
    expect(cell?.textContent).toBe('uncapped')
  })

  it('keeps the dash for a pool that was not read', async () => {
    const exp = await opened('probe')
    const row = exp.querySelector(`tr[data-pool="${UNREAD}"]`)!
    expect(row.textContent).not.toContain('uncapped')
    expect(row.textContent).toContain('—')
  })
})

describe('G5-18: the disabled sentence says its subject once and ends once', () => {
  it('renders a reason that already names the profile as it is, with one full stop', async () => {
    const exp = await opened('codex')
    const text = exp.textContent ?? ''
    expect(text).toContain('codex is disabled on this platform. Use claude-code.')
    expect(text).not.toMatch(/codex is disabled: codex is disabled/)
    expect(text).not.toContain('..')
  })

  it('prefixes a reason that does not name the profile, and strips its own full stop first', () => {
    expect(disabledSentence('codex', 'switched off. Use claude-code.')).toBe('codex is disabled: switched off. Use claude-code.')
    expect(disabledSentence('codex', 'codex is disabled on this platform.')).toBe('codex is disabled on this platform.')
    expect(disabledSentence('codex', 'codex is off')).toBe('codex is off.')
    expect(disabledSentence('codex', '')).toBe('codex is disabled: refused by the platform.')
    // A name that is only a prefix of the reason's first word is not the subject.
    expect(disabledSentence('code', 'codex shares its pools')).toBe('code is disabled: codex shares its pools.')
  })
})
