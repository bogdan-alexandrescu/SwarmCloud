import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Button } from './components'

/**
 * The last line of the same rule the rest of this app is built on.
 *
 * Without it, a render error unmounts the whole tree and the page goes WHITE --
 * no message, no retry, nothing. That is the purest form of this platform's
 * defining bug: a failure that renders as an absence. A blank page and "there
 * is no data" are indistinguishable to the person looking at them, and the
 * blank one is the more dangerous because it looks like a slow load.
 *
 * Observed for real during QA on 2026-09-19: a throw inside `Screen` took the
 * entire UI to a white page, and React's own console message was the only
 * evidence anything had happened.
 */
interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Kept in the console for whoever is debugging; never shown raw to a user
    // beyond the message, because a stack can carry data from the response.
    console.error('UI crashed:', error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    // THE TAKEOVER SHAPE (states.html C, §14): the one app-wide state left,
    // drawn as every whole-app state is -- the cause, the line that this says
    // nothing about the platform, and the one action.
    return (
      <div className="app">
        <section className="app-takeover is-bad" role="alert" aria-labelledby="ui-crashed-h">
          <h1 id="ui-crashed-h">The interface crashed</h1>
          <p>Something in this page threw an error and it could not finish rendering.</p>
          <p className="app-takeover-inv">
            This is a bug in the UI, not a statement about the platform. Whatever is running is still running.
          </p>
          <Button kind="primary" onClick={() => window.location.reload()}>
            Reload
          </Button>
          <p className="app-takeover-foot">{this.state.error.message}</p>
        </section>
      </div>
    )
  }
}
