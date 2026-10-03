/**
 * THE CANONICAL COMPONENTS (components.html Variant A, owner's pick
 * 2026-10-02), as behaviour.
 *
 * Each `it` names the property a reader relies on and the mutation that turns
 * it red: a disabled button that does not say why, an unknown figure drawn as
 * 0, a typed confirm that unlocks on "close enough", a failure that toasts,
 * a hatch on a measured bar.
 */
import STYLES from '../styles.css?raw'
import COMPONENTS_CSS from '../styles/components.css?raw'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  Banner,
  Breadcrumb,
  CancelWorkflowConfirm,
  cancelNeedsTypedConfirm,
  Button,
  Card,
  CardLink,
  Chip,
  CodeBlock,
  Count,
  Dash,
  DiffBlock,
  Dialog,
  EmptyState,
  LoadingState,
  Menu,
  ParkPill,
  PARK_WORD,
  Segmented,
  StatePill,
  StatTile,
  Table,
  Tabs,
  Toaster,
  Tooltip,
  TOAST_MS,
  TypedConfirm,
  UsageBar,
  acknowledge,
  forgetToasts,
  parseDiff,
  usageForm,
  type Column,
  type SortState,
} from '../components'
import { STATE_MARK } from '../marks'
import { PARK_NEEDS_A_PERSON, PARK_REASONS, type TaskState } from '../types'
import { expectNoFigures } from './setup'

afterEach(() => {
  forgetToasts()
  vi.useRealTimers()
  document.documentElement.removeAttribute('data-theme')
})

const ALL_STATES = Object.keys(STATE_MARK) as TaskState[]

// ---------------------------------------------------------------------------
// Tokens
// ---------------------------------------------------------------------------

function withSheet(theme: 'light' | 'dark'): () => void {
  const el = document.createElement('style')
  el.textContent = STYLES
  document.head.appendChild(el)
  document.documentElement.setAttribute('data-theme', theme)
  return () => el.remove()
}

const token = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

describe('the tokens are on :root, for light and dark', () => {
  it('draws the hairline in the Open sky values, not the old 3:1 grey (#503 "Palette")', () => {
    // MUTATION: put --line back to #698198 / #5f7087.
    let off = withSheet('light')
    expect(token('--line')).toBe('#d6e0ea')
    off()
    off = withSheet('dark')
    expect(token('--line')).toBe('#1e3350')
    off()
  })

  it('keeps a 3:1 control border as its own token', () => {
    let off = withSheet('light')
    expect(token('--ctl-bd')).toBe('#7d8ea3')
    off()
    off = withSheet('dark')
    expect(token('--ctl-bd')).toBe('#5f7087')
    off()
  })

  it('states the radius scale 4 / 8 / 9 / 12 / 14 / 999 and the 6px bar', () => {
    const off = withSheet('dark')
    expect(['--r-xs', '--r-ctl', '--r-tile', '--r-card', '--r-dlg', '--r-pill'].map(token)).toEqual(['4px', '8px', '9px', '12px', '14px', '999px'])
    expect(token('--track-h')).toBe('6px')
    expect([token('--h-sm'), token('--h-ctl'), token('--h-lg'), token('--row')]).toEqual(['28px', '32px', '36px', '32px'])
    off()
  })

  it('hatches with the control border, so "not measured" is a visible stripe', () => {
    expect(STYLES).toMatch(/--ctl-hatch: repeating-linear-gradient\([^;]*var\(--ctl-bd\)/)
    // And only the "not measured" forms use the hatch in the component sheet.
    const users = [...COMPONENTS_CSS.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{[^{}]*var\(--ctl-hatch\)[^{}]*\}/g)].map((m) => m[1]!.trim())
    // ProgressBar's unread steps (visual QA Q9) are counted but not measured:
    // the hatch's one meaning, on the third form that has it.
    expect(users).toEqual(['.c-bar.is-unmeasured', '.c-bar.is-unknown-ceiling > .c-h', '.c-bar.is-progress > .c-h'])
  })

  it('draws a control border with --ctl-bd and a card outline with the hairline', () => {
    const rule = (sel: string) => new RegExp(`(?:^|\\n)${sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{[^}]*\\}`).exec(COMPONENTS_CSS)?.[0] ?? ''
    expect(rule('.c-btn')).toContain('border: 1px solid var(--ctl-bd)')
    expect(rule('.c-seg')).toContain('var(--ctl-bd)')
    expect(rule('.c-card')).toContain('border: 1px solid var(--line)')
    expect(rule('.c-tbl')).toContain('border: 1px solid var(--line)')
  })
})

// ---------------------------------------------------------------------------
// Buttons
// ---------------------------------------------------------------------------

describe('Button', () => {
  it('draws five kinds as five classes, and the default as no modifier', () => {
    render(
      <>
        <Button kind="primary">Submit</Button>
        <Button>Retry</Button>
        <Button kind="ghost">Cancel</Button>
        <Button kind="danger">Stop</Button>
        <Button kind="danger-filled">Stop agent</Button>
      </>,
    )
    const cls = screen.getAllByRole('button').map((b) => b.className)
    expect(cls).toEqual(['c-btn is-primary', 'c-btn', 'c-btn is-ghost', 'c-btn is-danger', 'c-btn is-danger-filled'])
  })

  it('says why it is disabled, next to it, and is described by the reason', () => {
    // MUTATION: drop the reason span.
    render(<Button disabledReason="finished agents only">Resubmit</Button>)
    const b = screen.getByRole('button', { name: 'Resubmit' })
    expect(b).toHaveProperty('disabled', true)
    const why = document.getElementById(b.getAttribute('aria-describedby')!)
    expect(why?.textContent).toBe('finished agents only')
  })

  it('is busy: disabled, aria-busy, and says what it is doing', () => {
    const go = vi.fn()
    render(
      <Button kind="primary" busy="Submitting…" onClick={go}>
        Submit
      </Button>,
    )
    const b = screen.getByRole('button')
    expect(b.textContent).toBe('Submitting…')
    expect(b.getAttribute('aria-busy')).toBe('true')
    fireEvent.click(b)
    expect(go).not.toHaveBeenCalled()
  })

  it('an icon-only button keeps its label as its accessible name', () => {
    render(
      <Button iconOnly icon={<svg />}>
        Copy link
      </Button>,
    )
    expect(screen.getByRole('button', { name: 'Copy link' }).textContent).toBe('')
  })
})

// ---------------------------------------------------------------------------
// States and park reasons
// ---------------------------------------------------------------------------

describe('StatePill and ParkPill', () => {
  it('draws every task state in all three forms, with its own mark and hue', () => {
    for (const s of ALL_STATES) {
      const { container, unmount } = render(
        <>
          <StatePill state={s} form="table" />
          <StatePill state={s} form="pill" />
          <StatePill state={s} form="mark" />
        </>,
      )
      const marks = [...container.querySelectorAll('[data-mark]')].map((e) => e.getAttribute('data-mark'))
      expect(marks).toEqual([STATE_MARK[s].mark, STATE_MARK[s].mark, STATE_MARK[s].mark])
      const pill = container.querySelector('.c-pill')!
      expect(pill.className).toContain(`is-${STATE_MARK[s].hue}`)
      // Mark-only still carries the word, as its accessible name.
      expect(container.textContent).toContain(s.toLowerCase().replace('_', '-'))
      unmount()
    }
  })

  it('names every one of the eight park reasons in plain words, the code in the tooltip', () => {
    expect(Object.keys(PARK_WORD).sort()).toEqual([...PARK_REASONS].sort())
    for (const r of PARK_REASONS) {
      const { container, unmount } = render(<ParkPill reason={r} until="resumes 14:32" />)
      expect(container.querySelector('.c-pr')?.textContent).toBe(PARK_WORD[r])
      expect(container.querySelector('.c-pill')?.getAttribute('title')).toContain(r)
      expect(container.textContent).toContain('resumes 14:32')
      unmount()
    }
  })

  it('flags exactly the three reasons that need a person', () => {
    const flagged = PARK_REASONS.filter((r) => {
      const { container, unmount } = render(<ParkPill reason={r} />)
      const f = container.querySelector('[data-flag="needs-a-person"]') !== null
      unmount()
      return f
    })
    expect(flagged.sort()).toEqual([...PARK_NEEDS_A_PERSON].sort())
  })

  it('prints an unknown reason verbatim, and a missing one as "reason not recorded"', () => {
    const { container, rerender } = render(<ParkPill reason="NEW_REASON_2027" />)
    expect(container.querySelector('.c-pr')?.textContent).toBe('NEW_REASON_2027')
    rerender(<ParkPill reason={null} />)
    expect(container.querySelector('.c-pr')?.textContent).toBe('reason not recorded')
  })
})

// ---------------------------------------------------------------------------
// Chips, figures and cards: the unknown form is a dash with its reason
// ---------------------------------------------------------------------------

describe('an unknown figure is a dash with its reason, never 0', () => {
  it('Count draws null as a dash, named by its reason', () => {
    const { container } = render(<Count n={null} label="cost" why="the provider did not report" />)
    expectNoFigures(container)
    expect(container.textContent).toContain('—')
    expect(container.querySelector('[aria-label]')?.getAttribute('aria-label')).toBe('cost: the provider did not report')
  })

  it('Count draws a measured 0 as 0', () => {
    const { container } = render(<Count n={0} label="failed" />)
    expect(container.textContent).toBe('0 failed')
  })

  it('Dash carries its reason', () => {
    render(<Dash why="not measured anywhere" />)
    expect(screen.getByRole('img', { name: 'not measured anywhere' }).textContent).toBe('—')
  })

  it('StatTile draws the five honest forms differently, and the unknown one with no figure', () => {
    const { container } = render(
      <div>
        <StatTile label="Retries" tile={{ kind: 'value', value: 0, note: 'measured: none needed' }} />
        <StatTile label="CPU" tile={{ kind: 'unknown', why: 'not measured anywhere' }} />
        <StatTile label="Cost today" tile={{ kind: 'partial', value: '≥ $41.20', missed: '2 of 14 agents unread' }} />
        <StatTile label="Checkpoints" tile={{ kind: 'loading' }} />
        <StatTile label="Attempts" tile={{ kind: 'failed', why: 'HTTP 503', onRetry: () => {} }} />
      </div>,
    )
    const forms = [...container.querySelectorAll('.c-tile')].map((t) => t.getAttribute('data-form'))
    expect(forms).toEqual(['value', 'unknown', 'partial', 'loading', 'failed'])
    const unknown = container.querySelector('[data-form="unknown"]') as HTMLElement
    expectNoFigures(unknown)
    expect(unknown.className).toContain('is-unknown')
    expect(unknown.textContent).toContain('not measured anywhere')
    expect(container.querySelector('[data-form="failed"]')?.textContent).toContain('Could not read')
  })

  it('a card has its head, its one link out in sky sans, and its provenance foot', () => {
    const { container } = render(
      <Card title="Running now" action={<CardLink href="/agents/live">All live</CardLink>} foot="Read 4s ago from /v1/tasks">
        <p>rows</p>
      </Card>,
    )
    expect(container.querySelector('.c-card-h h2')?.textContent).toBe('Running now')
    const a = container.querySelector('a.c-link.is-card')!
    expect(a.textContent).toBe('All live →')
    expect(container.querySelector('.c-card-foot')?.textContent).toBe('Read 4s ago from /v1/tasks')
  })

  it('a filter chip removes itself; a link chip is a link', () => {
    const off = vi.fn()
    render(
      <>
        <Chip onRemove={off} removeLabel="Remove filter state: failed">
          state: failed
        </Chip>
        <Chip href="/capacity/pools?pool=global">pool:global</Chip>
      </>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter state: failed' }))
    expect(off).toHaveBeenCalledOnce()
    expect(screen.getByRole('link', { name: 'pool:global' }).getAttribute('href')).toBe('/capacity/pools?pool=global')
  })
})

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

interface Row {
  id: string
  name: string
  mins: number
}
const ROWS: Row[] = [
  { id: 'a', name: 'Fix flaky lease heartbeat test', mins: 18 },
  { id: 'b', name: 'Refactor quota broker', mins: 1 },
]
const COLS: Column<Row>[] = [
  { key: 'name', head: 'Agent', cell: (r) => r.name, sortable: true },
  { key: 'mins', head: 'Elapsed', cell: (r) => `${r.mins}m`, num: true, sortable: true },
]

function SortingTable() {
  const [sort, setSort] = useState<SortState>({ key: 'mins', dir: 'desc' })
  const rows = [...ROWS].sort((x, y) => (sort.key === 'mins' ? x.mins - y.mins : x.name.localeCompare(y.name)) * (sort.dir === 'asc' ? 1 : -1))
  return <Table caption="Agents" columns={COLS} rows={rows} rowKey={(r) => r.id} sort={sort} onSort={setSort} expand={(r) => <p>detail of {r.name}</p>} sticky />
}

describe('Table', () => {
  it('heads are sentence case, sortable through a button, and say their order to a screen reader', () => {
    const { container } = render(<SortingTable />)
    const heads = [...container.querySelectorAll('th[scope="col"]')]
    expect(heads.map((h) => h.textContent)).toEqual(['Agent', 'Elapsed'])
    expect(heads.map((h) => h.getAttribute('aria-sort'))).toEqual(['none', 'descending'])
    fireEvent.click(screen.getByRole('button', { name: 'Elapsed' }))
    expect(container.querySelectorAll('th[scope="col"]')[1]!.getAttribute('aria-sort')).toBe('ascending')
    expect([...container.querySelectorAll('tbody td:nth-child(2)')].map((td) => td.textContent)).toEqual(['Refactor quota broker', 'Fix flaky lease heartbeat test'])
  })

  it('opens one row at a time', () => {
    const { container } = render(<SortingTable />)
    const [first, second] = screen.getAllByRole('button', { name: 'Open row' })
    fireEvent.click(first!)
    expect(container.querySelectorAll('tr.c-xrow')).toHaveLength(1)
    fireEvent.click(second!)
    expect(container.querySelectorAll('tr.c-xrow')).toHaveLength(1)
    expect(container.querySelector('tr.c-xrow')?.textContent).toContain('Refactor quota broker')
  })

  it('has a sticky head and right-aligned figures', () => {
    const { container } = render(<SortingTable />)
    expect(container.querySelector('.c-tbl')?.className).toContain('is-sticky')
    expect(COMPONENTS_CSS).toMatch(/\.c-tbl th \{[^}]*position: sticky; top: 0/)
    expect(container.querySelector('td.is-num')?.textContent).toMatch(/m$/)
  })

  it('loads as three skeleton rows at the row height, never a spinner', () => {
    const { container } = render(<Table caption="Agents" columns={COLS} rows={[]} rowKey={(r) => r.id} loading />)
    expect(container.querySelectorAll('tbody tr')).toHaveLength(3)
    expect(container.querySelector('.c-spin')).toBeNull()
    expect(container.querySelector('table')?.getAttribute('aria-busy')).toBe('true')
  })
})

// ---------------------------------------------------------------------------
// Tabs, segmented, breadcrumb
// ---------------------------------------------------------------------------

describe('Tabs, Segmented and Breadcrumb', () => {
  it('a tab is a link to its own route, and a tab with nothing behind it shows a dash', () => {
    const go = vi.fn()
    render(
      <Tabs
        label="Agent"
        current="details"
        onGo={go}
        tabs={[
          { key: 'details', label: 'Details', href: '/agents/live/t1' },
          { key: 'attempts', label: 'Attempts', href: '/agents/live/t1/attempts', count: 1 },
          { key: 'logs', label: 'Logs', href: '/agents/live/t1/logs', count: null },
        ]}
      />,
    )
    const links = screen.getAllByRole('link')
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/agents/live/t1', '/agents/live/t1/attempts', '/agents/live/t1/logs'])
    expect(links[0]!.getAttribute('aria-current')).toBe('page')
    expect(links[2]!.textContent).toBe('Logs—')
    fireEvent.click(links[1]!)
    expect(go).toHaveBeenCalledWith('/agents/live/t1/attempts')
    // A modified click is the browser's: a new tab, not a route change.
    fireEvent.click(links[2]!, { ctrlKey: true })
    expect(go).toHaveBeenCalledTimes(1)
  })

  it('the tablist form is tab buttons, the selected one aria-selected, a count its reason in the title', () => {
    // The agent split's panes (#503 swap of `.ag-tabs`): Children has no
    // route, so the strip is a tablist that reports the key pressed.
    // MUTATION: drop the `onSelect` branch and the strip renders links.
    const pick = vi.fn()
    render(
      <Tabs
        label="Agent panes"
        current="attempts"
        onSelect={pick}
        tabs={[
          { key: 'detail', label: 'Details' },
          { key: 'attempts', label: 'Attempts', count: 2 },
          { key: 'artifacts', label: 'Artifacts', count: null, why: 'not known yet' },
        ]}
      />,
    )
    const list = screen.getByRole('tablist', { name: 'Agent panes' })
    expect(list.classList.contains('c-tabs')).toBe(true)
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false'])
    expect(tabs[0]!.querySelector('em'), 'a tab with no count draws none').toBeNull()
    expect(tabs[2]!.querySelector('em')?.textContent).toBe('—')
    expect(tabs[2]!.querySelector('em')?.getAttribute('title')).toBe('not known yet')
    expect(screen.queryByRole('link')).toBeNull()
    fireEvent.click(tabs[2]!)
    expect(pick).toHaveBeenCalledWith('artifacts')
  })

  it('the segmented control is toggle buttons', () => {
    const pick = vi.fn()
    render(<Segmented label="Theme" value="light" onChange={pick} options={[{ key: 'light', label: 'Light' }, { key: 'dark', label: 'Dark' }]} />)
    expect(screen.getByRole('button', { name: 'Light' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Dark' }))
    expect(pick).toHaveBeenCalledWith('dark')
  })

  it('the segmented control between addressed views is links, the current one aria-current', () => {
    // Capacity's Ceilings | By runner profile (the 2026-10-03 swap of CapSeg).
    // MUTATION: drop the link form and the views render as buttons again.
    render(<Segmented label="Pools views" value="a" options={[{ key: 'a', label: 'Ceilings', href: '#capacity/pools' }, { key: 'b', label: 'By runner profile', href: '#capacity/profiles' }]} />)
    const nav = screen.getByRole('navigation', { name: 'Pools views' })
    const links = [...nav.querySelectorAll('a')]
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['#capacity/pools', '#capacity/profiles'])
    expect(links[0]!.getAttribute('aria-current')).toBe('page')
    expect(links[1]!.getAttribute('aria-current')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('the breadcrumb links back and ends on the object, not a link', () => {
    render(<Breadcrumb crumbs={[{ key: 'w', label: 'Work', href: '/agents' }, { key: 'a', label: 'Agents', href: '/agents/live' }, { key: 'o', label: 'task_01JB', href: null, id: true }]} />)
    expect(screen.getAllByRole('link').map((a) => a.textContent)).toEqual(['Work', 'Agents'])
    expect(screen.getByText('task_01JB').getAttribute('aria-current')).toBe('page')
  })
})

// ---------------------------------------------------------------------------
// Tooltip, menu, dialog, typed confirm
// ---------------------------------------------------------------------------

describe('Tooltip, Menu and Dialog', () => {
  it('a tooltip shows on focus at once and on hover after 120ms', () => {
    vi.useFakeTimers()
    render(
      <Tooltip tip="Peak resident memory of the worker, sampled every 5s.">
        <button type="button">Peak memory</button>
      </Tooltip>,
    )
    const tip = screen.getByRole('tooltip', { hidden: true })
    expect(tip.hidden).toBe(true)
    fireEvent.mouseEnter(tip.parentElement!)
    act(() => vi.advanceTimersByTime(100))
    expect(tip.hidden).toBe(true)
    act(() => vi.advanceTimersByTime(30))
    expect(tip.hidden).toBe(false)
    fireEvent.mouseLeave(tip.parentElement!)
    expect(tip.hidden).toBe(true)
    fireEvent.focus(screen.getByRole('button'))
    expect(tip.hidden).toBe(false)
  })

  it('a menu is a disclosure, and a disabled item says why', () => {
    const copy = vi.fn()
    render(<Menu label="Actions" items={[{ key: 'c', label: 'Copy link', onSelect: copy }, { key: 'r', label: 'Resubmit', disabledReason: 'finished agents only' }]} />)
    const opener = screen.getByRole('button', { name: 'Actions' })
    expect(opener.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(opener)
    expect(document.querySelector('[role="menu"]')).toBeNull()
    const re = screen.getByRole('button', { name: /Resubmit/ })
    expect(re).toHaveProperty('disabled', true)
    expect(re.textContent).toContain('finished agents only')
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    expect(copy).toHaveBeenCalledOnce()
  })

  it('a dialog is modal, closes on Escape and gives focus back', () => {
    function Host() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Stop
          </button>
          {open && (
            <Dialog title="Stop this agent?" onClose={() => setOpen(false)} actions={<Button kind="danger-filled">Stop agent</Button>}>
              <p>Its last checkpoint is kept.</p>
            </Dialog>
          )}
        </>
      )
    }
    render(<Host />)
    const opener = screen.getByRole('button', { name: 'Stop' })
    opener.focus()
    fireEvent.click(opener)
    const dlg = screen.getByRole('dialog', { name: 'Stop this agent?' })
    expect(dlg.getAttribute('aria-modal')).toBe('true')
    expect(dlg.contains(document.activeElement)).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('the typed confirm unlocks only on an exact match', () => {
    // MUTATION: compare trimmed or case-folded.
    const yes = vi.fn()
    render(
      <TypedConfirm title="Cancel this workflow?" name="wf_2c81d0a9b7e4" verb="Cancel the workflow" keep="Keep it running" onConfirm={yes} onClose={() => {}}>
        <p>2 running steps are asked to stop at their next heartbeat.</p>
      </TypedConfirm>,
    )
    const field = screen.getByLabelText(/to confirm/)
    const verb = screen.getByRole('button', { name: 'Cancel the workflow' })
    expect(document.activeElement).toBe(field)
    for (const near of ['wf_2c81d0a9b7e', 'WF_2C81D0A9B7E4', ' wf_2c81d0a9b7e4', 'wf_2c81d0a9b7e4 ']) {
      fireEvent.change(field, { target: { value: near } })
      expect(verb).toHaveProperty('disabled', true)
      fireEvent.keyDown(field, { key: 'Enter' })
    }
    expect(yes).not.toHaveBeenCalled()
    fireEvent.change(field, { target: { value: 'wf_2c81d0a9b7e4' } })
    expect(verb).toHaveProperty('disabled', false)
    fireEvent.click(verb)
    expect(yes).toHaveBeenCalledOnce()
  })
})

// ---------------------------------------------------------------------------
// Banners and toasts
// ---------------------------------------------------------------------------

describe('Banner and Toast', () => {
  it('a failure banner is an alert and keeps its way out', () => {
    render(
      <Banner tone="bad" title="Could not read capacity." actions={<Button size="sm">Retry</Button>}>
        HTTP 503 from /v1/capacity. Other panels are current.
      </Banner>,
    )
    const b = screen.getByRole('alert')
    expect(b.className).toBe('c-banner is-bad')
    expect(b.textContent).toContain('Other panels are current.')
  })

  it('a toast acknowledges the viewer’s action, carries one action, and goes after 4s', () => {
    vi.useFakeTimers()
    const back = vi.fn()
    render(<Toaster />)
    act(() => {
      acknowledge('Now acting as platform.', { label: 'Back to eng', onClick: back })
    })
    expect(screen.getByText('Now acting as platform.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Back to eng' }))
    expect(back).toHaveBeenCalledOnce()
    expect(screen.queryByText('Now acting as platform.')).toBeNull()
    act(() => {
      acknowledge('Link copied')
    })
    act(() => vi.advanceTimersByTime(TOAST_MS - 10))
    expect(screen.queryByText('Link copied')).not.toBeNull()
    act(() => vi.advanceTimersByTime(20))
    expect(screen.queryByText('Link copied')).toBeNull()
  })

  it('has no failure toast to reach for: acknowledge takes no tone', () => {
    // A failure is a Banner. `acknowledge`'s second argument is an action,
    // and there is no third.
    expect(acknowledge.length).toBe(2)
    expect(COMPONENTS_CSS).not.toMatch(/\.c-toast\.is-(bad|warn|error)/)
  })
})

// ---------------------------------------------------------------------------
// Usage bars
// ---------------------------------------------------------------------------

describe('UsageBar', () => {
  it('names each form by its numbers, and a null is never a 0', () => {
    expect(usageForm(17, 40)).toBe('ok')
    expect(usageForm(11, 12)).toBe('nearly-full')
    expect(usageForm(4, 4)).toBe('full')
    expect(usageForm(5, 4)).toBe('over')
    expect(usageForm(9, null)).toBe('unknown-ceiling')
    expect(usageForm(null, 40)).toBe('unmeasured')
  })

  it('hatches only what is not a measurement', () => {
    const { container } = render(
      <div>
        <UsageBar label="pool:global" used={17} ceiling={40} />
        <UsageBar label="class:browser" used={11} ceiling={12} />
        <UsageBar label="Disk" used={null} ceiling={null} why="not measured" />
        <UsageBar label="pool:anthropic" used={9} ceiling={null} />
        <UsageBar label="account browser-acct-2" used={5} ceiling={4} />
      </div>,
    )
    const forms = [...container.querySelectorAll('.c-ub')].map((u) => u.getAttribute('data-form'))
    expect(forms).toEqual(['ok', 'nearly-full', 'unmeasured', 'unknown-ceiling', 'over'])
    const disk = container.querySelectorAll('.c-ub')[2] as HTMLElement
    expectNoFigures(disk)
    expect(disk.querySelector('[role="meter"]')).toBeNull()
    expect(container.querySelectorAll('.c-ub')[3]!.textContent).toContain('9 held · ceiling unknown')
    // Nearly full reads in greyscale: the triangle and the words.
    expect(container.querySelectorAll('.c-ub')[1]!.textContent).toContain('nearly full')
    expect(container.querySelectorAll('.c-ub')[1]!.querySelector('svg')).not.toBeNull()
    // The over bar puts its tick at the ceiling.
    expect(container.querySelector('.c-ub[data-form="over"] .c-tk')).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Empty, loading, code
// ---------------------------------------------------------------------------

describe('EmptyState, LoadingState, CodeBlock and DiffBlock', () => {
  it('draws empty, partial and failed as three kinds, and loading with no spinner', () => {
    const { container } = render(
      <div>
        <EmptyState kind="empty" heading="No agents waiting">
          Nothing is queued, ready or parked in tenant eng.
        </EmptyState>
        <EmptyState kind="partial" heading="Read 6 of 7 sources">
          Holders were refused: admin only.
        </EmptyState>
        <EmptyState kind="failed" heading="Could not read agents">
          This is a failed read, not an empty list.
        </EmptyState>
        <LoadingState />
      </div>,
    )
    expect([...container.querySelectorAll('.c-emp')].map((e) => e.getAttribute('data-kind'))).toEqual(['empty', 'partial', 'failed', 'loading'])
    expect(container.querySelector('.c-spin')).toBeNull()
    expect(screen.getByRole('alert').textContent).toContain('failed read')
  })

  it('numbers code lines from the first line given', () => {
    const { container } = render(<CodeBlock title="agent_worker/lease.py" lang="python" text={'def renew(lease, now):\n    pass\n'} firstLine={41} />)
    expect([...container.querySelectorAll('.c-ln')].map((n) => n.textContent)).toEqual(['41', '42'])
  })

  it('marks added and removed lines with + and −, so a diff reads without colour', () => {
    const patch = '@@ -88,3 +88,4 @@ def beat(self):\n lease = self.store.read(self.task_id)\n-self.store.renew(lease)\n+with self.store.transaction() as tx:\n+    tx.renew(lease)\n'
    expect(parseDiff(patch).map((l) => l.kind)).toEqual(['hunk', 'ctx', 'del', 'add', 'add'])
    const { container } = render(<DiffBlock file="agent_worker/heartbeat.py" patch={patch} />)
    expect(container.querySelector('.c-ds')?.textContent).toBe('+2 −1')
    const added = container.querySelector('[data-line="add"]')!
    expect(added.textContent).toContain('+ with self.store')
    expect(container.querySelector('[data-line="del"]')!.textContent).toContain('− self.store.renew')
  })
})

// ---------------------------------------------------------------------------
// The typed confirm on Cancel workflow (states.html C, §12)
// ---------------------------------------------------------------------------

describe('CancelWorkflowConfirm', () => {
  it('asks for the typed id only while a step is running, and treats unknown as running', () => {
    expect(cancelNeedsTypedConfirm(2)).toBe(true)
    expect(cancelNeedsTypedConfirm(0)).toBe(false)
    expect(cancelNeedsTypedConfirm(null)).toBe(true)
  })

  it('says what stops, what is cancelled and what is kept, and unlocks on the exact id', () => {
    const yes = vi.fn()
    render(<CancelWorkflowConfirm workflowId="wf_2c81d0a9b7e4" running={2} notStarted={1} onConfirm={yes} onClose={() => {}} />)
    const dlg = screen.getByRole('dialog', { name: 'Cancel this workflow?' })
    expect(dlg.textContent).toContain('2 running steps are asked to stop at their next heartbeat; 1 step not yet started is cancelled. Steps that finished keep their results.')
    const verb = screen.getByRole('button', { name: 'Cancel the workflow' })
    expect(verb).toHaveProperty('disabled', true)
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: 'wf_2c81d0a9b7e4' } })
    fireEvent.click(verb)
    expect(yes).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: 'Keep it running' })).toBeTruthy()
  })

  it('says an unknown count as unknown, never as 0', () => {
    render(<CancelWorkflowConfirm workflowId="wf_x" running={null} notStarted={null} onConfirm={() => {}} onClose={() => {}} />)
    const text = screen.getByRole('dialog').textContent ?? ''
    expect(text).toContain('an unknown number of running steps')
    expect(text).not.toMatch(/\b0 /)
  })
})
