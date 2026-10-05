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
import { cleanup, configure } from '@testing-library/react'

import { forgetProbes } from '../fetch'

// 2000 ms for findBy*/waitFor, twice Testing Library's default and no more (#605). #591 set
// 8000 after qa.u11a.logs and qa.u10a.agent timed out on unrelated branches' runs on 2026-10-05.
// The cause was the waits, not the box: each poll of a `findByRole` over the agent split cost
// ~850 ms of `getComputedStyle` even idle, so those waits took 1.5-1.9 s in a full run on
// 2026-10-05 and a loaded runner fit one poll in the window. They now wait on the read their
// assertion depends on (`landed` in reads.ts) and query once. A ceiling this high stays only
// as headroom for a runner busier than the one measured; a wait that needs more is a test to
// fix like those, not a reason to raise this. Files that pass their own `{ timeout }` keep it.
configure({ asyncUtilTimeout: 2000 })

const forbidden = (): never => {
  throw new Error(
    'a test reached the network: the UI suite is offline by contract. ' +
      'Stub globalThis.fetch with vi.fn() in the test that needs a response.',
  )
}

// THE OTHER GLOBAL THIS SUITE HAS TO PUT BACK. `restoreMocks` and
// `unstubGlobals` undo what a test did to a mock or a global; neither of them
// knows about the API probe registry in `fetch.ts`, which is module state and
// therefore outlives the test that filled it -- Vitest isolates per FILE, not
// per test. A test that renders `<App />` and lets a fixture read land was
// leaving a "newest read just now" behind for the next test in the file, and
// `shell.test.tsx`'s B3 assertion -- that the head says "nothing has loaded"
// when nothing has -- failed on every run of that file by itself and passed in
// the full suite only on timing. See `forgetProbes` for the long version.
beforeEach(() => {
  globalThis.fetch = forbidden as unknown as typeof fetch
  forgetProbes()
  // REAL ROUTES (rebrand 2026-10-01): the router writes the PATH now, so a
  // test that opened an agent would otherwise leave the next test's <App />
  // on that agent. Every test starts at the root, as a fresh tab would.
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  cleanup()
  // AFTER `cleanup`, so nothing is still subscribed when the registry is
  // emptied. The probe registry is module state in fetch.ts and therefore
  // outlives a test: without this, a screen rendered in one test leaves its
  // successful reads registered for every test after it in the same file,
  // and "nothing has loaded in this tab" becomes untestable. See the header
  // of `forgetProbes`.
  forgetProbes()
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
