// Per-test setup, and the thing that makes "this suite is offline" a fact.
//
// THE OFFLINE GUARD. `make test` is offline by contract -- no credentials, no
// emulator, no network. Node 20 ships a real global `fetch`, and jsdom does not
// take it away, so a test that forgets to stub it does not fail: it tries to
// resolve a relative URL against jsdom's `http://localhost:3000` and hangs or
// reaches a machine. Replacing the global with one that throws makes any
// un-stubbed read an immediate, named failure instead.
//
// `restoreMocks` + `unstubGlobals` in vitest.config.ts put this back between
// tests, so the guard is in force at the start of every one of them rather
// than only the first.

import { afterEach, beforeEach, expect } from 'vitest'
import { cleanup } from '@testing-library/react'

const forbidden = (): never => {
  throw new Error(
    'a test reached the network: the UI suite is offline by contract. ' +
      'Stub globalThis.fetch with vi.fn() in the test that needs a response.',
  )
}

beforeEach(() => {
  globalThis.fetch = forbidden as unknown as typeof fetch
})

afterEach(() => {
  cleanup()
})

// A shared assertion, because it is the rule this whole product is built on
// and it is easy to state weakly. "No zero" means no DIGIT anywhere in the
// rendered subtree: a failure surface that prints "0 agents", "0 of 8" or even
// "HTTP 503" beside a count is the exact bug -- a read that failed, rendered as
// a measurement. HTTP status codes are allowed through explicitly, because
// they are provenance about the request rather than a figure about the
// platform.
export function expectNoFigures(el: HTMLElement, allow: readonly string[] = []): void {
  let text = el.textContent ?? ''
  for (const a of allow) text = text.split(a).join('')
  const digits = text.match(/\d+/g) ?? []
  expect(digits, `a failure surface rendered the number(s) ${digits.join(', ')}`).toEqual([])
}
