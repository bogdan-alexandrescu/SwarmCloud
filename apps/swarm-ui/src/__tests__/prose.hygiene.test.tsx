// FOUR LEFTOVERS FROM THE RESTRAINT PASS, EACH NAMED IN design-system.md AS
// "NOT FIXED" BY THE LANE THAT FOUND IT, AND EACH NOW ASSERTED.
//
//   §11.3 / §12.5  the help `?` is a bordered ring -- a pill -- drawn by
//                  HelpCard.tsx's inline style, after `.ctl-q-glyph` had
//                  already become a borderless disc (§13.3).
//   §12.5          Liveness puts a 17-word sentence in the drawer's heading:
//                  "No event for 3m. Heartbeat events are only every ~150s, so
//                  this is not yet alarming."
//   §12.5          TimeSeries' nothing-measured state is a 40-word paragraph.
//                  §6.9's shape is mark, heading, ONE sentence, a link out.
//   §12.5          `.ctl-subnav` draws the drawer's two panes as filled pills,
//                  which §6.11 settles as `.ctl-seg`'s job.
//
// Each test says what it would take to break it.

import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'

import APP from '../App.tsx?raw'
import STYLES from '../styles.css?raw'
import { HelpCard } from '../HelpCard'
import { LivenessBadge } from '../Liveness'
import { TimeSeries } from '../charts/TimeSeries'
import { absent } from '../charts/series'
import { stripComments } from './spaceprobe'
import type { Task, TaskEvent, TaskState } from '../types'

// ---------------------------------------------------------------------------
// The `?`
// ---------------------------------------------------------------------------

/** The inline style React would write on the `?`, as a property map. */
function glyphStyle(): Map<string, string> {
  // SERVER MARKUP, NOT jsdom. React assigns inline styles through
  // `element.style`, and jsdom's CSSOM silently drops a `border` shorthand it
  // cannot parse -- `1px solid var(--line)` is one -- so reading the style
  // back off a mounted button would report no border on the very glyph this
  // test is about. The server renderer writes the object out verbatim.
  //
  // It also warns that `useEdgeSafePlacement`'s useLayoutEffect "does nothing
  // on the server", which is true and irrelevant: placement is measured on
  // open, and this reads the closed glyph. That one warning is swallowed so the
  // CI log does not carry a stack trace nobody needs to chase; anything else
  // still reaches stderr.
  const original = console.error
  const quiet = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    if (typeof args[0] === 'string' && args[0].includes('useLayoutEffect does nothing on the server')) return
    original(...args)
  })
  let html: string
  try {
    html = renderToStaticMarkup(<HelpCard topic="capacity" />)
  } finally {
    quiet.mockRestore()
  }
  const m = /<button[^>]*\sstyle="([^"]*)"/.exec(html)
  expect(m, 'the help card rendered no styled button').not.toBeNull()
  const out = new Map<string, string>()
  for (const decl of m![1]!.split(';')) {
    const at = decl.indexOf(':')
    if (at > 0) out.set(decl.slice(0, at).trim(), decl.slice(at + 1).trim())
  }
  return out
}

describe('the help `?` is a disc, not a ring', () => {
  it('draws no border', () => {
    // BREAK IT: put `border: '1px solid var(--line)'` back on GLYPH.
    const s = glyphStyle()
    for (const prop of ['border', 'border-width', 'border-style', 'border-color']) {
      const v = s.get(prop)
      if (v === undefined) continue
      expect(v, `the ? still draws ${prop}: ${v}`).toMatch(/^(0(px)?|none)(\s|$)/)
    }
  })

  it('is still something to find: a filled disc', () => {
    // BREAK IT: take the ring off and leave `background: transparent`, which
    // would make the glyph a bare `?` floating in a line of text -- the fix
    // bought by making the affordance vanish.
    const s = glyphStyle()
    const fill = s.get('background') ?? s.get('background-color') ?? ''
    expect(fill, 'the ? lost its ring and gained nothing in its place').not.toMatch(/^(transparent|none)?$/)
  })
})

// ---------------------------------------------------------------------------
// Liveness
// ---------------------------------------------------------------------------

const NOW = Date.UTC(2026, 8, 23, 12, 0, 0)

function task(state: TaskState): Task {
  return {
    id: 'task_0123456789abcdef0123',
    tenant_id: 'acme',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: new Date(NOW - 3_600_000).toISOString(),
    updated_at: new Date(NOW).toISOString(),
    started_at: new Date(NOW - 3_600_000).toISOString(),
    completed_at: state === 'SUCCEEDED' ? new Date(NOW - 60_000).toISOString() : null,
    submitted_by: 'ada@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: 'lse_1',
  }
}

function eventAgo(seconds: number): TaskEvent[] {
  return [
    {
      event_id: 'ev_1',
      task_id: 'task_0123456789abcdef0123',
      type: 'heartbeat',
      at: new Date(NOW - seconds * 1000).toISOString(),
      attempt_id: 'att_1',
      lease_id: 'lse_1',
      generation: 1,
      detail: null,
    },
  ]
}

/** Runs of two or more letters: `12s`, `3m` and `14:03:07` are data. */
function words(text: string): number {
  return (text.match(/[A-Za-z][A-Za-z'’-]+/g) ?? []).length
}

const CASES: ReadonlyArray<{ name: string; state: TaskState; events: TaskEvent[] | null; say: RegExp }> = [
  { name: 'live', state: 'RUNNING', events: eventAgo(12), say: /12s/ },
  { name: 'quiet', state: 'RUNNING', events: eventAgo(240), say: /heartbeat/i },
  { name: 'silent', state: 'RUNNING', events: eventAgo(540), say: /reconciler/i },
  { name: 'unknown (read failed)', state: 'RUNNING', events: null, say: /could not be read/i },
  { name: 'unknown (no timestamp)', state: 'RUNNING', events: [{ ...eventAgo(0)[0]!, at: 'not a date' }], say: /timestamp/i },
  { name: 'finished', state: 'SUCCEEDED', events: eventAgo(60), say: /finished/i },
  { name: 'not started', state: 'READY', events: [], say: /dispatched/i },
]

describe('the liveness badge is a word and a figure, not a sentence', () => {
  for (const c of CASES) {
    it(`${c.name}: at most three words on the glass, the sentence on the accessible route`, () => {
      // BREAK IT: put "Heartbeat events are only every ~150s, so this is not
      // yet alarming." back into the visible copy.
      const { container } = render(<LivenessBadge task={task(c.state)} events={c.events} now={NOW} />)
      const badge = container.querySelector('.liveness')
      expect(badge).not.toBeNull()
      const copy = badge!.querySelector('.lv-copy')?.textContent ?? ''
      expect(words(copy), `"${copy}" is prose in a heading`).toBeLessThanOrEqual(3)
      // The words did not vanish: a keyboard and a screen reader reach the
      // whole explanation, which a `title=` alone never gave them.
      expect(badge!.getAttribute('aria-label') ?? '').toMatch(c.say)
    })
  }
})

// ---------------------------------------------------------------------------
// TimeSeries, nothing measured
// ---------------------------------------------------------------------------

describe("a chart with nothing measured says so in §6.9's shape", () => {
  const T0 = Date.UTC(2026, 8, 22, 10, 0, 0)
  function nothing() {
    return render(
      <TimeSeries
        points={[
          absent(T0, 'attempt 1', 'this attempt has not finished.'),
          absent(T0 + 3_600_000, 'attempt 2', 'this attempt never started.'),
        ]}
        title="Token spend, attempt by attempt"
        noun="attempt"
        format={(v) => `$${v.toFixed(2)}`}
        absentCopy="an absent measurement, not $0.00. Hover a hatched band for the reason that attempt has no figure."
        zero="anchored"
      />,
    )
  }

  it('is a heading, one sentence and a link out', () => {
    // BREAK IT: append `{absentCopy}` to the sentence again.
    const { container } = nothing()
    const empty = container.querySelector('.ctl-empty')
    expect(empty, 'the nothing-measured state is not an empty state').not.toBeNull()
    expect(empty!.querySelector('h3')?.textContent).toMatch(/nothing was measured/i)
    const paragraphs = [...empty!.querySelectorAll('p')]
    expect(paragraphs, 'one sentence, in one paragraph').toHaveLength(1)
    const sentences = (paragraphs[0]!.textContent ?? '')
      .split(/[.!?](?:\s|$)/)
      .map((s) => s.trim())
      .filter((s) => s !== '')
    expect(sentences, `"${paragraphs[0]!.textContent}" is more than one sentence`).toHaveLength(1)
    // The explanation is a destination, not a second paragraph.
    expect(empty!.querySelector('a[href^="#help/"]'), 'no link out to the explanation').not.toBeNull()
  })

  it('does not tell the reader to use marks it did not draw', () => {
    // The class-level copy for this series is written for the PLOTTED case,
    // where absences are hatched bands. With nothing measured there is no
    // plot, so "hover a hatched band" is an instruction with no object.
    const { container } = nothing()
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent ?? '').not.toMatch(/hover/i)
  })
})

// ---------------------------------------------------------------------------
// The drawer's pane strip
// ---------------------------------------------------------------------------

describe("the agent drawer's panes are one segmented control, not pills", () => {
  it('renders Detail / Attempts as the .ctl-seg primitive', () => {
    // BREAK IT: drop `ctl-seg` from the strip's className in App.tsx.
    const m = /<div className="([^"]*)"\s+role="tablist"\s+aria-label="Agent panes"/.exec(APP)
    expect(m, 'App.tsx no longer renders the Agent panes tablist this test knows').not.toBeNull()
    expect(m![1]!.split(/\s+/)).toContain('ctl-seg')
  })

  it('leaves no pill treatment behind for the strip', () => {
    // BREAK IT: restore `.ctl-subnav button { border-radius: 999px }`. With
    // the strip also a `.ctl-seg`, that later rule would out-rank the
    // primitive and draw the pills again.
    expect(stripComments(STYLES)).not.toMatch(/\.ctl-subnav\s+button/)
  })
})
