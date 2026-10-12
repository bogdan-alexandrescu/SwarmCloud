// A SCREEN WHOSE `go` MOVES IT, for the Automate tests (live QA AUT-11,
// 2026-10-11). A schedule's tab and its editor are in the address now, so a
// click on Gate or Edit is a `go` -- and a test that stubs `go` with a spy
// that goes nowhere would never see the tab it clicked. `Routed` keeps the
// view the screen was last sent to, for the screen's own section, and still
// records every `go` on the spy the test asserts on.

import { useState, type ComponentType } from 'react'

export function Routed({
  Screen,
  section,
  start,
  spy,
}: {
  Screen: ComponentType<{ view: string | null; go: (to: string) => void }>
  /** The address the screen answers, `automate/schedules`; any other `go` is only recorded. */
  section: string
  start: string | null
  spy: (to: string) => void
}) {
  const [view, setView] = useState<string | null>(start)
  const go = (to: string) => {
    spy(to)
    const q = to.indexOf('?')
    const bare = q === -1 ? to : to.slice(0, q)
    if (bare === section) setView(q === -1 ? null : to.slice(q + 1))
  }
  return <Screen view={view} go={go} />
}
