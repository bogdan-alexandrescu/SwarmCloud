// QA1B-13 (part of #2596, the console visual QA pass 1 epic): pass 1 lanes
// wrote these fixes as patches for types.ts, help.ts and styles.css because
// those files were outside their territory. This file holds each one.
//
//   capacity-21         Pools' Set by said `AIMD back-off`: the algorithm's
//                       name, not what happened. It says the ceiling was
//                       lowered automatically after errors (QA1-FIX-03).
//   capacity-14         a backend pool printed the API's enum,
//                       `CLOUD_RUN_JOB`, as its name: poolLabel says
//                       `Cloud Run` / `GKE Autopilot` (QA1-FIX-08).
//   admin-shell-17      no help topic said where the platform owner and the
//                       config-group admins come from: People and roles'
//                       scope topic names PLATFORM_OWNER and ADMIN_GROUPS
//                       (QA1-FIX-17).
//   runs-submit         Repositories' help named an `Add new repository`
//                       button the screen no longer draws; it names
//                       `Choose more on GitHub` (QA1-FIX-21).
//   overview-agents-59  the Now card drew a not-started task's state word in
//                       the mono 600 reading face; it is the line's plain
//                       sans (QA1-FIX-32).
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HELP } from '../help'
import { LivenessBadge } from '../Liveness'
import { poolLabel, setBy } from '../types'
import type { Pool, Task } from '../types'
import { painted } from './marks'

afterEach(cleanup)

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'tenant:eng',
    hard_limit: 10,
    effective_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    ...over,
  } as Pool
}

describe('capacity-21: a ceiling AIMD lowered says so in words', () => {
  it('names neither AIMD nor back-off', () => {
    const by = setBy(pool({ adaptive_target: 3, effective_limit: 3 }))
    // MUTATION: restore `AIMD back-off`.
    expect(by.term).toBe('lowered automatically after errors')
    expect(by.detail).toBe('Lowered automatically to 3 after provider errors; configured is 10.')
    expect(`${by.term} ${by.detail}`).not.toMatch(/AIMD|back-off/i)
  })
})

describe('capacity-14: a backend pool is named in words', () => {
  it.each([
    ['backend:CLOUD_RUN_JOB', 'Cloud Run'],
    ['backend:GKE_AUTOPILOT', 'GKE Autopilot'],
    // A backend this console has no word for keeps its id rather than a guess.
    ['backend:SOMETHING_NEW', 'SOMETHING_NEW'],
  ] as ReadonlyArray<[string, string]>)('%s -> %s', (name, label) => {
    expect(poolLabel(name)).toBe(label)
  })

  it('leaves every other kind as it was', () => {
    expect(poolLabel('tenant:eng')).toBe('eng')
    expect(poolLabel('provider:anthropic:tenant:u-x')).toBe('anthropic · u-x')
  })
})

describe('admin-shell-17: help says where the platform roles come from', () => {
  it('People and roles names PLATFORM_OWNER and ADMIN_GROUPS', () => {
    const text = HELP['people-and-roles-scope'].long.join(' ')
    expect(text).toContain('PLATFORM_OWNER')
    expect(text).toContain('ADMIN_GROUPS')
    expect(text).toContain('the Admins card does not list them')
  })
})

describe('Repositories help names the button the screen draws', () => {
  it('says Choose more on GitHub, not Add new repository', () => {
    const short = HELP['config-repositories'].short
    expect(short).toContain('Choose more on GitHub (Choose on GitHub while nothing is chosen) opens GitHub access')
    expect(short).not.toContain('Add new repository')
  })
})

describe('overview-agents-59: a not-started state word is the plain sans', () => {
  const task = (state: string) => ({ task_id: 'task_1', state }) as unknown as Task

  it('draws `ready` in the line face, not the mono reading face', () => {
    const { container } = render(<LivenessBadge task={task('READY')} events={[]} now={Date.now()} />)
    const badge = container.querySelector('.liveness[data-liveness="not-started"]')!
    expect(badge, 'no not-started badge').not.toBeNull()
    const word = badge.querySelector('.lv-word')!
    expect(word.textContent).toBe('ready')
    // MUTATION: drop the not-started rule.
    expect(painted(word, 'font-family', { width: 1440 })).toBe('var(--font)')
    expect(painted(word, 'font-weight', { width: 1440 })).toBe('400')
  })

  it('keeps a live reading in the mono face', () => {
    const { container } = render(<LivenessBadge task={task('RUNNING')} events={null} now={Date.now()} />)
    const word = container.querySelector('.liveness .lv-word')!
    expect(painted(word, 'font-family', { width: 1440 })).not.toBe('var(--font)')
  })
})
