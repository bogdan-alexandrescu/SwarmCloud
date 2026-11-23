import { Component, type ReactNode } from 'react'
import { Button } from './components'

/**
 * A STALE PAGE IS NOT A CRASH (issue 194).
 *
 * After a deploy the image ships only the new `dist`, so a tab opened on the
 * old build asks for a hashed chunk that 404s, and the route's lazy `import()`
 * rejects. Without this boundary that rejection reached the root
 * `ErrorBoundary` and replaced the whole console with "The interface crashed
 * … This is a bug in the UI": wrong on both counts, nothing is broken, the
 * page is out of date. This one sits round each route's `Suspense`, inside the
 * content area, so the shell stays up and the page says what is true, with the
 * one action that fixes it.
 *
 * ONLY A CHUNK-LOAD ERROR is caught. Anything else is thrown again from
 * `render`, so it reaches the root boundary exactly as before.
 *
 * `React.lazy` keeps the rejected promise: rendering the same lazy component
 * again throws the same error, so a retry in place cannot work. Reload fetches
 * the new build; a change of `resetKey` (the route's address) clears the
 * state, so another page, whose chunk may well be cached, can still open.
 */

// Chrome and Edge, Firefox, Safari: the TypeError each throws when a dynamic
// `import()` cannot fetch its module. `ChunkLoadError` is the name webpack-style
// loaders give the same failure.
const CHUNK_MESSAGES = [
  /Failed to fetch dynamically imported module/i,
  /error loading dynamically imported module/i,
  /Importing a module script failed/i,
]

export function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  if (error.name === 'ChunkLoadError') return true
  return CHUNK_MESSAGES.some((re) => re.test(error.message))
}

interface Props {
  /** The route's address: the boundary clears when it changes. */
  resetKey: string
  children: ReactNode
}

interface State {
  error: unknown
  failedAt: string | null
}

export class ChunkBoundary extends Component<Props, State> {
  state: State = { error: null, failedAt: null }

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error }
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (state.error === null) return null
    // The first pass after a catch records where it failed; a later address clears it.
    if (state.failedAt === null) return { failedAt: props.resetKey }
    if (state.failedAt !== props.resetKey) return { error: null, failedAt: null }
    return null
  }

  render() {
    const { error } = this.state
    if (error === null) return this.props.children
    // Not ours: hand it on, unchanged, to the root ErrorBoundary.
    if (!isChunkLoadError(error)) throw error
    return (
      <p className="ctl-empty" role="status" data-chunk-stale="">
        A newer version is out.{' '}
        <Button kind="primary" size="sm" onClick={() => window.location.reload()}>
          Reload
        </Button>
      </p>
    )
  }
}
