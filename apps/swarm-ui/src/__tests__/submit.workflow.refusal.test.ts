// A WORKFLOW THE API REFUSED AT SUBMISSION READS AS REFUSED.
//
// WHY THIS FILE EXISTS (#64). The API refuses a workflow step that stages one
// filename from two parents, or an absolute or traversing filename, at
// submission, with HTTP 422 `invalid_dag`: the same status and code as every
// sibling DAG refusal (a cycle, a dangling dependency). The owner chose 422 on
// #64 for exactly the reason this file holds: `KIND_BY_STATUS` maps 422 to
// "invalid", so the screen heads the refusal "That request was not valid" and
// prints the server's sentence, which names the step, the parents and the
// file. Any other status would fall through to `server_error` and a correct
// refusal would be headed "The API failed on this request".
//
// WHAT IS ASSERTED. `postWorkflow` is handed a real `Response`, as the browser
// would hand it one, and three things are checked together: the outcome is
// `rejected` (so the screen says nothing was created and offers the form back
// rather than "the workflow may exist"), the kind is `invalid`, and the
// server's sentence reaches the screen verbatim.

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
  it('HTTP 422 invalid_dag is an invalid request, not a failed API', async () => {
    respond(422, {
      code: 'invalid_dag',
      message: COLLISION,
      detail: { step_id: 'merge-1', filename: 'notes.md', colliding_upstream_steps: ['scan-A', 'scan-B'] },
    })

    const sub = await postWorkflow({ steps: [] })

    expect(sub.kind).toBe('rejected')
    if (sub.kind !== 'rejected') throw new Error(`expected a rejection, got ${sub.kind}`)
    expect(sub.error.kind).toBe('invalid')
    expect(errorHeading(sub.error)).toBe('That request was not valid')
    expect(sub.error.httpStatus).toBe(422)
    expect(sub.error.code).toBe('invalid_dag')
    // VERBATIM: the step, the parents and the file are the only part a person
    // can act on, and the screen prints this string as it arrived.
    expect(sub.error.message).toBe(COLLISION)
  })
})
