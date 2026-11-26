// The mounts for the openers no state opened before review round 2 of issue
// 439 (prosestates.openers.ts): each draws one screen on hand-built answers
// whose fold or dialog the fixture build never reaches, and the state's
// click opens it. Each slice that uses one restores with restoreOpenedStubs.

import type { ReactElement } from 'react'
import { vi } from 'vitest'

import type { ApiError } from '../fetch'
import { ITEM, comment, routes } from './planthreadfixture'
import { serve } from './repofixture'
import { codingRecords, stepsOf, transcriptOf } from './tracefixture'
import { task } from './runfixture'

const realFetch = globalThis.fetch
const restores: (() => void)[] = []

/**
 * The next restore Runner sizes sends is refused as a memory cut below the
 * 7-day peak of `classes` (admin/runner-sizes-restore-cut): api.ts's
 * restoreRunnerSizes, as the App the walk drew imported it, answers so until
 * restored. Called once the page is drawn, so the spy is on the module that
 * walk's App loaded.
 */
export async function refuseRestoreAsCut(classes: readonly string[]): Promise<void> {
  const api = await import('../api')
  const error = { kind: 'conflict', httpStatus: 409, code: 'below_recent_peak', message: 'below the recent peak', detail: { classes: Object.fromEntries(classes.map((c) => [c, {}])) } }
  const spy = vi.spyOn(api, 'restoreRunnerSizes').mockResolvedValue({ status: 'error', error: error as unknown as ApiError })
  restores.push(() => spy.mockRestore())
}

/** Undo every stub a mount here set: called after each state. */
export function restoreOpenedStubs(): void {
  while (restores.length > 0) restores.pop()?.()
  globalThis.fetch = realFetch
  vi.unstubAllEnvs()
  vi.resetModules()
}

/** The phone's media query matches, until restored (jsdom has no matchMedia). */
function stubPhone(): void {
  const had = Object.getOwnPropertyDescriptor(window, 'matchMedia')
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }),
  })
  restores.push(() => {
    if (had === undefined) delete (window as { matchMedia?: unknown }).matchMedia
    else Object.defineProperty(window, 'matchMedia', had)
  })
}

/** Config › GitHub's policy card on a phone: each rule folded to its head. */
export async function mountPhonePolicy(): Promise<ReactElement> {
  stubPhone()
  vi.resetModules()
  const { AccessPolicy } = await import('../Access')
  return <AccessPolicy />
}

/** An approval drawn live on `handler`'s answers (planthreadfixture's routes). */
async function liveApproval(handler: Parameters<typeof serve>[0]): Promise<ReactElement> {
  serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  return <ApprovalsScreen view={`approval=${encodeURIComponent(ITEM)}`} go={() => undefined} />
}

/** A plan whose step 2 has one resolved comment: folded under "1 resolved". */
export function mountResolvedComment(): Promise<ReactElement> {
  return liveApproval(
    routes({ run: () => ({ plan_thread: [comment(), comment({ entry_id: 'pth_done1', text: 'Name the bucket.', resolved: true })] }) }),
  )
}

/** A plan whose planner wrote a design doc: folded under "Design doc and diagrams". */
export function mountDesignDoc(): Promise<ReactElement> {
  const docs = {
    task_id: 'task_planner1',
    design_docs: [{ name: 'design.md', bytes: 40, content_type: 'text/markdown', read_url: '/x' }],
    diagrams: [],
  }
  return liveApproval(
    routes({
      run: () => ({ planner_documents: docs }),
      extra: (m, url) =>
        m === 'GET' && url.split('?')[0] === '/v1/tasks/task_planner1/artifacts/content'
          ? { status: 200, body: { status: 'ok', content: '# Design\n\nThe overlap read.', truncated: false } }
          : null,
    }),
  )
}

/** The theater's timeline after a browser attached: its protocol calls folded into one group. */
export async function mountTheaterProtocol(): Promise<ReactElement> {
  const t0 = Date.parse('2026-10-12T15:05:00Z')
  const actions = ['Browser.getVersion', 'Target.setAutoAttach', 'Runtime.enable', 'Page.enable'].map((method, i) => ({
    at: new Date(t0 + i * 1000).toISOString(),
    kind: 'cdp',
    method,
  }))
  globalThis.fetch = vi.fn(
    async () => new Response(JSON.stringify({ attempt_id: 'att_fold', actions }), { status: 200, headers: { 'content-type': 'application/json' } }),
  ) as unknown as typeof fetch
  vi.resetModules()
  const { TheaterTimeline } = await import('../TheaterTimeline')
  return <TheaterTimeline taskId="task_live" live={false} />
}

/** A coding agent's trace: its Edit step's reply folded under "The tool's reply". */
export async function mountTraceEdit(): Promise<ReactElement> {
  const { Trace } = await import('../trace/Trace')
  const read = transcriptOf(stepsOf(codingRecords()))
  return (
    <Trace
      task={task({ id: 'task_trace', state: 'SUCCEEDED' })}
      streams={null}
      attemptId={null}
      fromStart={false}
      onFromStart={() => undefined}
      query=""
      onQuery={() => undefined}
      load={() => Promise.resolve({ status: 'ok', fetchedAt: Date.now(), data: read })}
    />
  )
}
