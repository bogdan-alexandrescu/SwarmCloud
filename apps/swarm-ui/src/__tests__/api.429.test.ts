// WHOSE 429 IT WAS (ANSWERS-429; part of #1816).
//
// A 429 from the API's own token bucket (`swarm_api.errors.RateLimited`,
// served as `{"code": "rate_limited", "message": ..., "detail":
// {"retry_after_seconds": ...}}`) and a 429 from Cloud Run's front end ("no
// available instance") read the same in the console: "you are polling too
// much". The console's read now tells them apart by the body, as the bridge
// does (`swarm_mcp.client.ApiRateLimited` / `NoInstanceFree`): the API's own
// keeps its message, anything else says Cloud Run had no instance free. The
// kind stays `rate_limited` for both, so the pause and its countdown are
// unchanged.
//
// Every case is a real `Response` handed to the real `read`, which is what
// every api.ts loader goes through.

import { describe, expect, it, vi } from 'vitest'

import { NO_INSTANCE_FREE, read, route } from '../fetch'

const API_OWN = { code: 'rate_limited', message: 'rate limit exceeded for this principal', detail: { retry_after_seconds: 0.05 } }

function respond(status: number, body: string, headers: Record<string, string>) {
  globalThis.fetch = vi.fn(async () => new Response(body, { status, headers })) as unknown as typeof fetch
}

async function errorOf() {
  const got = await read(route('/v1/capacity'), () => false)
  if (got.status !== 'error') throw new Error('expected an error, got ' + got.status)
  return got.error
}

describe('a 429 says whose it was', () => {
  it("keeps the API's own refusal in its own words", async () => {
    respond(429, JSON.stringify(API_OWN), { 'content-type': 'application/json', 'retry-after': '2' })
    const error = await errorOf()
    // MUTATION: reading every 429 as Cloud Run's rewrites the API's message.
    expect(error.kind).toBe('rate_limited')
    expect(error.code).toBe('rate_limited')
    expect(error.message).toBe('rate limit exceeded for this principal')
    expect(error.retryAfterSeconds).toBe(2)
  })

  it.each([
    ['text', 'Rate exceeded.', 'text/plain'],
    ['html', '<html><body>429 Too Many Requests</body></html>', 'text/html'],
    ['fastapi detail', JSON.stringify({ detail: 'rate limit exceeded' }), 'application/json'],
    ['another code', JSON.stringify({ code: 'quota_exceeded', message: 'no' }), 'application/json'],
  ])("reads any other 429 (%s) as Cloud Run's front end", async (_name, body, type) => {
    respond(429, body, { 'content-type': type })
    const error = await errorOf()
    // MUTATION: classifying a 429 by status alone leaves today's message here.
    expect(error.message).toBe(NO_INSTANCE_FREE)
    expect(error.message).toContain("SwarmCloud's API had no instance free (Cloud Run); retried")
    // MUTATION: a new kind would stop the Shell's pause: it stays rate_limited.
    expect(error.kind).toBe('rate_limited')
  })
})
