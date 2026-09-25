// A REFUSED WORKFLOW READS AS REFUSED, WHICHEVER 4xx THE API CHOSE.
//
// WHY THIS FILE EXISTS (#64). The API now refuses a workflow step that stages
// one filename from two parents, or an absolute or traversing filename, at
// submission, with HTTP 400 `invalid_dag`. That status is the owner's, from
// the brief for #64. The New Workflow screen mapped only 422 to "invalid", so
// a 400 fell through `KIND_BY_STATUS` to `server_error` and was headed "The
// API failed on this request": a correct refusal, naming the step, the parents
// and the file, presented as a broken platform. The first cut of PR #65 dodged
// that by returning 422 instead, which revised the owner's decision rather
// than the screen.
//
// WHAT IS ASSERTED. `postWorkflow` is handed a real `Response`, as the browser
// would hand it one, and three things are checked together: the outcome is
// `rejected` (so the screen says nothing was created and offers the form back
// rather than "the workflow may exist"), the kind is `invalid`, and the
// server's sentence reaches the screen verbatim. 422 is held beside it, so a
// fix that moved the 400 by breaking the 422 cannot pass.

import { describe, expect, it, vi } from 'vitest'

import { errorHeading } from '../fetch'
import { postWorkflow } from '../SubmitWorkflow'

const COLLISION =
  "step 'merge-1' stages 'notes.md' from 2 upstream steps ('scan-A', 'scan-B'). " +
  'Give each parent a distinct artifact filename and stage those instead.'

function respond(status: number, body: unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })),
  )
}

describe('a workflow the API refused at submission', () => {
  it.each([400, 422])('HTTP %i invalid_dag is an invalid request, not a failed API', async (status) => {
    respond(status, {
      code: 'invalid_dag',
      message: COLLISION,
      detail: { step_id: 'merge-1', filename: 'notes.md', colliding_upstream_steps: ['scan-A', 'scan-B'] },
    })

    const sub = await postWorkflow({ steps: [] })

    expect(sub.kind).toBe('rejected')
    if (sub.kind !== 'rejected') throw new Error(`expected a rejection, got ${sub.kind}`)
    expect(sub.error.kind).toBe('invalid')
    expect(errorHeading(sub.error)).toBe('That request was not valid')
    expect(sub.error.httpStatus).toBe(status)
    expect(sub.error.code).toBe('invalid_dag')
    // VERBATIM: the step, the parents and the file are the only part a person
    // can act on, and the screen prints this string as it arrived.
    expect(sub.error.message).toBe(COLLISION)
  })
})
