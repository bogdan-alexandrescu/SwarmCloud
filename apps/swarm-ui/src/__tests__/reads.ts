// WAIT ON THE READ, NOT ON THE QUERY (#605).
//
// A `findByRole` or a `waitFor` whose callback calls `getByRole` re-runs that
// query on every poll and every DOM mutation, and a `*ByRole` query is the
// most expensive thing this suite does: to decide what is accessible it calls
// `getComputedStyle` on each candidate and its ancestors, against the ~240KB of
// component CSS that `css: true` injects (vitest.config.ts says why that is
// on). Measured 2026-10-05 on an idle 6-core box: one `getAllByRole` over the
// 209-node agent split took 810-870 ms. A 1000 ms `findBy` window therefore
// held one or two polls, and on a loaded CI runner none that came after the
// read landed -- `qa.u10a` D36 and `qa.u11a`'s "Last log line" cases went red
// on PRs that touched no UI, and #591 raised the suite-wide timeout to 8 s to
// hide it.
//
// `landed` waits on the thing the assertion actually depends on: the mocked
// read was called, and its answer has been handed to React. Its wait callback
// reads a call count, which costs nothing, so the window it needs is the
// render chain's and not the query's. The role query then runs ONCE, after,
// as a plain synchronous `getByRole` -- it still asserts the element's role,
// name and accessibility, and no timeout can expire while it runs.
//
// Only for a read the test made resolve or reject (`mockResolvedValue`,
// `mockRejectedValue`): a read stubbed as `new Promise(() => {})` never
// settles, and waiting on it would hang until the test's own timeout.

import { act, waitFor } from '@testing-library/react'
import type { Mock } from 'vitest'

export async function landed(read: Mock, calls = 1): Promise<void> {
  await waitFor(() => {
    const n = read.mock.calls.length
    if (n < calls) throw new Error(`${read.getMockName()} was called ${n} time(s); waiting for ${calls}`)
  })
  await act(async () => {
    await Promise.allSettled(read.mock.results.map((r) => r.value))
    // The component's own `.then` chain (useRead, Screen) is a few microtasks
    // behind the mock's promise; one macrotask lets all of it run, inside
    // `act`, so React has committed what the answer draws before this returns.
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}
