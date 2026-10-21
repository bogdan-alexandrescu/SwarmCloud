// QAFIX2-FIXTURE-EXIT (part of #1680): visual QA run 2 finding WG-4.
//
// The preview build's `fixtureAttempts` gave every finished task's newest
// attempt exit code 0 -- including a FAILED task whose `last_error` reads
// "exit status 1". QA reported the contradiction, and it is one the real API
// never serves for that case: the worker writes the failing exit code on the
// attempt and the same failure into the task's `last_error`.
//
// These read what the fixture SERVES, through the real `loadTasks` and
// `loadAttempts` in the fixture build, rather than a hand-built row: the
// defect was in the fixture, and a hand-built row would agree with whatever
// the test author assumed.

import { describe, expect, it } from 'vitest'

import { loadAttempts, loadTasks } from '../api'
import type { AttemptRow, Task } from '../types'

async function fixtureTasks(): Promise<Task[]> {
  // The fixture branch is taken only in a DEV build without VITE_LIVE. Said
  // here so a change to that switch fails this file by name.
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
  const r = await loadTasks()
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') throw new Error('unreachable')
  return r.data.tasks
}

/** Each task with its newest attempt, read in parallel: every fixture read carries its own simulated latency. */
async function withNewest(tasks: Task[]): Promise<[Task, AttemptRow][]> {
  return Promise.all(tasks.map(async (t): Promise<[Task, AttemptRow]> => [t, await newestAttempt(t.id)]))
}

async function newestAttempt(taskId: string): Promise<AttemptRow> {
  const r = await loadAttempts(taskId)
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') throw new Error('unreachable')
  expect(r.data.attempts.length, `${taskId} has no fixture attempts`).toBeGreaterThan(0)
  return r.data.attempts.reduce((a, b) => (b.generation > a.generation ? b : a))
}

/** The exit code a `last_error` names, or null when it names none. */
function namedExitCode(lastError: string | null): number | null {
  const m = lastError?.match(/\bexit (?:status|code) (\d+)\b/i)
  return m ? Number(m[1]) : null
}

describe('a fixture attempt exits the way its task ended', () => {
  it('a FAILED task\'s newest attempt exits non-zero', async () => {
    const failed = (await fixtureTasks()).filter((t) => t.state === 'FAILED')
    expect(failed.length, 'the fixture serves no FAILED task').toBeGreaterThan(0)
    let visited = 0
    for (const [t, newest] of await withNewest(failed)) {
      // MUTATION: restore `mk(3, live ? null : 0, ...)` in fixtureAttempts and
      // this fails -- a failed task's newest attempt read "exit 0" (WG-4).
      expect(newest.exit_code, `${t.id} failed, yet its newest attempt exited 0`).not.toBe(0)
      // MUTATION: give a failed task's newest attempt `null` and this fails.
      expect(newest.exit_code, `${t.id} failed, yet its newest attempt has no exit code`).not.toBeNull()
      visited++
    }
    expect(visited).toBe(failed.length)
  })

  it('a FAILED task\'s newest attempt exits with the code its last_error names', async () => {
    const failed = (await fixtureTasks()).filter((t) => t.state === 'FAILED')
    // At least one failure names a code other than 1, so a fixture that wrote
    // a constant 1 everywhere cannot pass the "its own code" half of this.
    // MUTATION: give every FAILED fixture task the same "exit status 1"
    // last_error and this fails.
    expect(
      failed.some((t) => (namedExitCode(t.last_error) ?? 1) !== 1),
      'no FAILED fixture task names an exit code other than 1',
    ).toBe(true)
    let checked = 0
    for (const [t, newest] of await withNewest(failed)) {
      const named = namedExitCode(t.last_error)
      if (named === null || named === 0) continue
      // MUTATION: hard-code the failed exit code to 1 and this fails on the
      // task whose last_error names exit status 2.
      expect(newest.exit_code, `${t.id}'s last_error names exit ${named}`).toBe(named)
      checked++
    }
    expect(checked, 'no FAILED fixture task names its exit code').toBeGreaterThan(0)
  })

  it('a SUCCEEDED task\'s newest attempt exits 0', async () => {
    const succeeded = (await fixtureTasks()).filter((t) => t.state === 'SUCCEEDED')
    expect(succeeded.length, 'the fixture serves no SUCCEEDED task').toBeGreaterThan(0)
    for (const [t, newest] of await withNewest(succeeded)) {
      // MUTATION: give every terminal task's newest attempt a non-zero exit
      // (for example `mk(3, live ? null : 1, ...)`) and this fails.
      expect(newest.exit_code, `${t.id} succeeded`).toBe(0)
    }
  })
})
