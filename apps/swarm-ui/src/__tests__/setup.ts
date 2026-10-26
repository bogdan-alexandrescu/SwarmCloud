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
// ~850 ms of `getComputedStyle` even idle, so qa.u11a's waits took up to 2.0 s and a loaded
// runner fit one poll in the window. They now wait on the read their assertion depends on
// (`landed` in reads.ts) and query once. Measured on 2026-10-05 over the whole suite (217
// files, 2 workers, 526 s -- the CI job's 517-572 s), no wait took over 1000 ms; the longest was
// 853 ms. So the per-file `{ timeout: 8000 }` limits sixteen agent-split and walkthrough files
// carried are gone too. This ceiling stays only as headroom for a runner busier than the one
// measured; a wait that needs more is a test to fix like those, not a reason to raise this.
configure({ asyncUtilTimeout: 2000 })

// THE ADVERSARIAL CLOCK (#2379, F10/F11; BGD chose "yes, in setup.ts" on
// 2026-10-26). Every test file starts at 23:30:00Z on today's UTC date, and
// the clock runs forward in real time from there. A test that reads the wall
// clock and assumes "now + 2h is still today" passed for 22 hours a day and
// failed for 2 (`utcAt` in accountsPool.ts prints a date when the instant is
// on another UTC day, on purpose), so the class reached main unseen. Here it
// fails on every run. Today's date is kept so fixtures dated near the real
// day stay as recent as they were; only the time of day is moved.
//
// ONCE PER FILE, AT LOAD, NOT PER TEST. Setup files run before each test
// file is imported, and fixtures read the clock at import
// (`export const NOW = Date.now()` in repofixture.ts and its siblings). A
// clock set in `beforeEach` would leave those on the wall clock while the page
// reads 23:30Z, hours apart, and every "expires in 9 days" would be red for a
// reason that is not a same-day assumption. Resetting it per test would also
// run time backwards between tests. A file runs in seconds, so 23:30Z leaves
// it half an hour before midnight, and a test that reaches past "+30 min" is
// across the day line, which is the point.
//
// WHY A DATE OF ITS OWN AND NOT `vi.useFakeTimers({ toFake: ['Date'] })`. In
// vitest 3.2.7 `useFakeTimers()` installs nothing while fake timers are
// already on (`if (!this._fakingTime)`), so a test calling `vi.useFakeTimers()`
// to fake `setTimeout` would silently keep real timers and fail for a reason
// that has nothing to do with the clock. Replacing only the global `Date`
// leaves that call working: vitest's clock installs over this one, starting
// from this one's `Date.now()`, and puts it back on `useRealTimers()`. A test
// that pins its own instant with `setSystemTime` is unaffected.
//
// AND NOT A `Proxy` over the real one: `vi.spyOn(Date, 'now')`, which
// workflow.board.test.tsx pins its clock with, would write the spy through to
// the real `Date` and read back this offset on top of it. `now` here is an own
// property, so a spy replaces it and `restoreMocks` puts it back, exactly as
// on the real `Date`.
//
// The real Date is kept on the global under a symbol: a run in one process
// (`singleFork`, or a worker reused across files) shares `globalThis`, so the
// next file's setup would otherwise take this file's clock for the real one.
const REAL_DATE = Symbol.for('swarm-ui.setup.realDate')
const realm = globalThis as typeof globalThis & { [REAL_DATE]?: DateConstructor }
const RealDate = (realm[REAL_DATE] ??= globalThis.Date)
const realNow = RealDate.now.bind(RealDate)
const loaded = new RealDate(realNow())
const clockOffset =
  RealDate.UTC(loaded.getUTCFullYear(), loaded.getUTCMonth(), loaded.getUTCDate(), 23, 30) - loaded.getTime()
const adversarialNow = (): number => realNow() + clockOffset
function AdversarialDate(this: unknown, ...args: unknown[]): Date | string {
  // `Date()` called without `new` returns the current time as a string.
  if (!new.target) return new RealDate(adversarialNow()).toString()
  return Reflect.construct(RealDate, args.length ? args : [adversarialNow()], new.target) as Date
}
// `UTC` and `parse` come through the constructor's prototype; `instanceof Date`
// holds both ways because the instances' prototype is the real one.
Object.setPrototypeOf(AdversarialDate, RealDate)
AdversarialDate.prototype = RealDate.prototype
AdversarialDate.now = adversarialNow
const ClockDate = AdversarialDate as unknown as DateConstructor
globalThis.Date = ClockDate

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
  // Back over the real Date only: `vi.useRealTimers()` after `vi.setSystemTime`
  // puts the real one back, and a file that faked timers in `beforeAll` keeps its own.
  if (globalThis.Date === RealDate) globalThis.Date = ClockDate
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
