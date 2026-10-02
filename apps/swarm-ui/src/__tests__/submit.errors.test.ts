// THE SUBMIT PATHS CLASSIFY A FAILURE THE WAY EVERY READ DOES (states.html,
// "Found while reading the code").
//
// Submit.tsx and SubmitWorkflow.tsx call `fetch` themselves, and each kept a
// status table of its own that mapped EVERY 503 to `upstream_degraded`. The
// API says which 503 it is: "tenant could not be resolved" / "group
// membership" is `tenant_unresolved`, anything else a degraded dependency. A
// submit during a tenant-resolution failure therefore told the person a
// service was down, when the fix is theirs (or an admin's) to make.
//
// MUTATION: put a status table back in either path and the first case fails.

import { describe, expect, it, vi } from 'vitest'

import { errorHeading } from '../fetch'
import { postTask } from '../Submit'
import { postWorkflow } from '../SubmitWorkflow'

function respond(status: number, body: unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })),
  )
}

const UNRESOLVED = { code: 'tenant_unresolved', message: 'The tenant could not be resolved: group membership lookup failed.' }
const DEGRADED = { code: 'upstream_unavailable', message: 'Firestore did not answer in time.' }
const ODD_403 = { code: 'forbidden', message: 'Something else entirely.' }

async function kindOfTask(): Promise<string> {
  const r = await postTask({ runner_profile: 'claude-code', input: {} })
  if (r.kind !== 'failed') throw new Error(`expected a failure, got ${r.kind}`)
  return r.error.kind
}

async function kindOfWorkflow(): Promise<string> {
  const r = await postWorkflow({ steps: [] })
  if (r.kind !== 'rejected' && r.kind !== 'uncertain') throw new Error(`expected a failure, got ${r.kind}`)
  return r.error.kind
}

describe.each([
  ['a task', kindOfTask],
  ['a workflow', kindOfWorkflow],
])('submitting %s', (_what, kindOf) => {
  it('reads a tenant-resolution 503 as tenant_unresolved', async () => {
    respond(503, UNRESOLVED)
    expect(await kindOf()).toBe('tenant_unresolved')
  })

  it('reads any other 503 as upstream_degraded', async () => {
    respond(503, DEGRADED)
    expect(await kindOf()).toBe('upstream_degraded')
  })

  it('reads an unrecognised 403 as a generic refusal, not as admin only', async () => {
    respond(403, ODD_403)
    expect(await kindOf()).toBe('forbidden')
    expect(errorHeading({ kind: 'forbidden', httpStatus: 403, code: 'forbidden', message: 'm' })).toBe('The API refused this request')
  })

  it('reads a disabled tenant as tenant_disabled', async () => {
    respond(403, { code: 'forbidden', message: 'Tenant eng is disabled.' })
    expect(await kindOf()).toBe('tenant_disabled')
  })
})
