// Issue 194: a failed lazy chunk load after a deploy must not crash the console.
//
// A tab opened on the old build asks for a hashed chunk the new image no
// longer ships; the `import()` rejects. That is "the page is out of date", not
// "the interface crashed": the boundary round each route's Suspense says so in
// the content area, with a Reload, and the shell round it stays up. Any other
// error still reaches the root ErrorBoundary.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { lazy, Suspense } from 'react'

import { ChunkBoundary, isChunkLoadError } from '../ChunkBoundary'
import { ErrorBoundary } from '../ErrorBoundary'
import { CHUNK_RELOAD_KEY, installPreloadReload, markLoadSucceeded } from '../preloadReload'

const reload = vi.fn()
const realLocation = window.location

beforeEach(() => {
  reload.mockReset()
  Object.defineProperty(window, 'location', { configurable: true, value: { ...realLocation, reload } })
  // React logs every caught error; the assertions are on what renders.
  vi.spyOn(console, 'error').mockImplementation(() => {})
  sessionStorage.clear()
})

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
  vi.restoreAllMocks()
})

function staleChunk() {
  return lazy(() => Promise.reject(new TypeError('Failed to fetch dynamically imported module: /assets/x-abc.js')))
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <ErrorBoundary>
      <nav>the shell</nav>
      <main>{children}</main>
    </ErrorBoundary>
  )
}

describe('ChunkBoundary', () => {
  it('shows "A newer version is out" with a Reload, and the shell stays mounted', async () => {
    const Stale = staleChunk()
    render(
      <Shell>
        <ChunkBoundary resetKey="/agents">
          <Suspense fallback={<p>Loading…</p>}>
            <Stale />
          </Suspense>
        </ChunkBoundary>
      </Shell>,
    )
    const line = await screen.findByRole('status')
    expect(line.textContent).toContain('A newer version is out')
    expect(screen.getByText('the shell')).toBeTruthy()
    expect(screen.queryByText('The interface crashed')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('lets any other error reach the root boundary', () => {
    function Throws(): never {
      throw new Error('a render bug')
    }
    render(
      <Shell>
        <ChunkBoundary resetKey="/agents">
          <Throws />
        </ChunkBoundary>
      </Shell>,
    )
    expect(screen.getByText('The interface crashed')).toBeTruthy()
    expect(screen.queryByText(/A newer version is out/)).toBeNull()
  })

  it('resets when the route address changes', async () => {
    const Stale = staleChunk()
    const { rerender } = render(
      <ChunkBoundary resetKey="/agents">
        <Suspense fallback={<p>Loading…</p>}>
          <Stale />
        </Suspense>
      </ChunkBoundary>,
    )
    await screen.findByRole('status')
    rerender(
      <ChunkBoundary resetKey="/capacity">
        <p>the next page</p>
      </ChunkBoundary>,
    )
    expect(screen.getByText('the next page')).toBeTruthy()
    expect(screen.queryByText(/A newer version is out/)).toBeNull()
  })

  it('knows each browser\'s chunk-load message and ChunkLoadError', () => {
    expect(isChunkLoadError(new TypeError('Failed to fetch dynamically imported module: /assets/a.js'))).toBe(true)
    expect(isChunkLoadError(new TypeError('error loading dynamically imported module: /assets/a.js'))).toBe(true)
    expect(isChunkLoadError(new TypeError('Importing a module script failed.'))).toBe(true)
    const named = new Error('Loading chunk 7 failed.')
    named.name = 'ChunkLoadError'
    expect(isChunkLoadError(named)).toBe(true)
    expect(isChunkLoadError(new TypeError('Failed to fetch'))).toBe(false)
    expect(isChunkLoadError(new Error('a render bug'))).toBe(false)
    expect(isChunkLoadError('not an error')).toBe(false)
  })
})

describe('the vite:preloadError handler', () => {
  it('reloads once, sets the flag, and does not reload a second time in the session', () => {
    const remove = installPreloadReload()
    try {
      const first = new Event('vite:preloadError', { cancelable: true })
      act(() => {
        window.dispatchEvent(first)
      })
      expect(first.defaultPrevented).toBe(true)
      expect(reload).toHaveBeenCalledTimes(1)
      expect(sessionStorage.getItem(CHUNK_RELOAD_KEY)).not.toBeNull()

      const second = new Event('vite:preloadError', { cancelable: true })
      act(() => {
        window.dispatchEvent(second)
      })
      expect(reload).toHaveBeenCalledTimes(1)
    } finally {
      remove()
    }
  })

  it('clears the flag after a successful load, so a later deploy reloads again', () => {
    sessionStorage.setItem(CHUNK_RELOAD_KEY, '1')
    markLoadSucceeded()
    expect(sessionStorage.getItem(CHUNK_RELOAD_KEY)).toBeNull()
  })
})
