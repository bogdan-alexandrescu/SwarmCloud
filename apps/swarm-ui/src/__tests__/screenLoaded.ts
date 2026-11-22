// WAIT FOR THE ROUTE'S SCREEN (issue 79). App.tsx loads each route's screen
// with React.lazy, so the first render of a route draws the shell and the
// Suspense fallback (`ScreenLoading`, marked `data-screen-loading`), and the
// screen a moment later. A test that reads the screen straight after
// `render(<App />)` awaits this first; what it then asserts is unchanged. Once
// a screen's module has loaded, its route renders at once.
//
// POLLED ON THE REAL CLOCK, NOT `waitFor`: some of these files run under
// `vi.useFakeTimers()`, where `waitFor`'s own timeout never fires, and the
// module load the screen waits on is I/O that no faked timer advances. The
// `setTimeout` kept here is the real one, taken before any test fakes it.

import { act } from '@testing-library/react'

const realSetTimeout = globalThis.setTimeout
const STEP_MS = 20

const loading = (): boolean => document.querySelector('[data-screen-loading]') !== null

/** Resolves once no route screen is still loading; fails if one never does. */
export async function screenLoaded(timeoutMs = 10_000): Promise<void> {
  for (let waited = 0; loading(); waited += STEP_MS) {
    if (waited >= timeoutMs) throw new Error(`a route screen was still loading after ${timeoutMs} ms`)
    await act(async () => {
      await new Promise((resolve) => realSetTimeout(resolve, STEP_MS))
    })
  }
}
