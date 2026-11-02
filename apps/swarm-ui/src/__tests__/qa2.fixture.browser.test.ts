// QA2 overview-agents-31 and -39 (owner's pick on #2704, 2026-10-29: apply
// outside-api.ts.patch): two findings that came from the fixture's data, not
// the console.
//
//   -31  the browser agent's tool calls carried `{ "step": n }`, so the
//        transcript's one-line gist (Artifacts.tsx `inputGist`) had no
//        address, element or text to show beside the tool's name.
//   -39  log and transcript ages were measured at FIXTURE_NOW, while the
//        console adds the time since the read and ages an attempt's end by
//        the browser's clock: a days-old object read `34m ago`.
//
// These read what the fixture SERVES, through the real loaders, as
// qafix4.fixture.data.test.ts does.

import { describe, expect, it } from 'vitest'

import { loadTaskLogs, loadTranscript } from '../api'
import { inputGist } from '../Artifacts'

const LONG = 60_000
const BROWSER_TASK = 'task_f523c160'

function ok<T>(r: { status: string; data?: T; error?: unknown }, what: string): T {
  expect(r.status, `${what} answered ${r.status}: ${JSON.stringify(r.error ?? null)}`).toBe('ok')
  return r.data as T
}

describe('overview-agents-31: the fixture browser agent’s tool calls carry their inputs', () => {
  it('every navigate, click and type call has a gist; a snapshot has none to give', async () => {
    expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
    const tr = ok(await loadTranscript(BROWSER_TASK), `GET /v1/tasks/${BROWSER_TASK}/transcript`)
    const calls = (tr.steps ?? []).filter((s) => s.kind === 'tool_call' && (s.tool?.name ?? '').startsWith('browser_'))
    expect(calls.length, 'the browser agent made no browser tool calls').toBeGreaterThan(0)
    let gisted = 0
    for (const s of calls) {
      const gist = inputGist(s.tool!.input ?? null)
      // MUTATION: put `{ "step": n }` back as the input -> every gist is null.
      if (s.tool!.name === 'browser_snapshot') continue
      expect(gist, `${s.tool!.name} (${s.tool!.input}) has no gist`).not.toBeNull()
      gisted += 1
    }
    expect(gisted).toBeGreaterThan(0)
    const navigate = calls.find((s) => s.tool!.name === 'browser_navigate')!
    expect(inputGist(navigate.tool!.input ?? null)).toMatch(/^https:\/\/example\.com\//)
  }, LONG)
})

describe('overview-agents-39: fixture ages are measured against the browser’s clock', () => {
  it('object_updated_at plus age_seconds is now, for the transcript and every log stream', async () => {
    const [transcript, logs] = await Promise.all([loadTranscript(BROWSER_TASK), loadTaskLogs(BROWSER_TASK)])
    const tr = ok(transcript, `GET /v1/tasks/${BROWSER_TASK}/transcript`)
    const lg = ok(logs, `GET /v1/tasks/${BROWSER_TASK}/logs`)
    const aged = [
      ['transcript', tr.stream.object_updated_at, tr.stream.age_seconds],
      ...lg.streams.map((s) => [`log ${s.stream}`, s.object_updated_at, s.age_seconds] as const),
    ] as const
    let visited = 0
    for (const [what, updated, age] of aged) {
      if (updated === null || updated === undefined || age === null || age === undefined) continue
      // MUTATION: measure against FIXTURE_NOW again -> off by the days since the fixture clock.
      const drift = Math.abs(Date.parse(updated) + age * 1000 - Date.now())
      expect(drift, `${what}: updated ${updated}, aged ${age}s`).toBeLessThan(LONG)
      visited += 1
    }
    expect(visited, 'no stream carried an age').toBeGreaterThan(0)
  }, LONG)
})
