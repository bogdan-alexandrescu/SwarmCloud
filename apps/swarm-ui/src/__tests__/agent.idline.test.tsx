// THE TASK ID IS A VISIBLE, COPYABLE SUB-LINE UNDER THE AGENT'S NAME (#94).
//
// #94 named an agent by its workflow step and asked that the task id stay as
// "a copyable sub-line". The 2026-10-05 sweep found the naming done and the
// sub-line not: the split header kept the id only in a copy button's tooltip
// (`AgCopyId`) and a list row only in a hover title, so reading an id off a
// screen -- to paste into a log query, a ticket, `sc` -- meant hovering for
// it. Now every agent row and the split header print the whole id, in mono,
// under the name, with a copy button; a refused clipboard selects the text so
// one keystroke still copies it; and a phone keeps it on one line, cut in the
// MIDDLE so both ends of the id stay readable, while the copy is always whole.
//
// MUTATIONS, one per block: drop `<TaskIdLine>` from the row (or the header);
// copy `shown` instead of `id`; skip the fallback's `selectNodeContents`;
// delete `white-space: nowrap` from `.tid-text`, or let `.tid-tail` shrink;
// drop `flex-direction: column` from `.row.is-compact > .cr-name`.

import AGENTS_CSS from '../styles/agents.css?raw'
import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'

import type { Task } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { at, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadResourceClasses: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { TaskRow } = await import('../Agents')
const { AgentSplit } = await import('../AgentSplit')

const ID = 'task_9c0ade75fd0c4f1c9a6e3b71'
const STEP = runTask({
  id: ID,
  workflow_id: 'wf_a25f6eb11a5b40bd8b58',
  step_id: 'scan-08',
  state: 'RUNNING',
  started_at: at(1),
  completed_at: null,
})
const LONE = runTask({ id: 'task_0123456789abcdef01234567', step_id: null, state: 'SUCCEEDED', started_at: at(1), completed_at: at(10) })

const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }
const SHEETS = AGENTS_CSS + '\n' + STYLES
const painted = (el: Element, prop: string, env: CascadeEnv): string | null => {
  const r = cascade(SHEETS, el, prop, env)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

function row(t: Task, onOpen: (id: string) => void = () => {}): HTMLElement {
  const { container } = render(<TaskRow task={t} now={Date.parse(at(5))} onOpen={onOpen} classes={null} />)
  return container.querySelector<HTMLElement>('.row.is-compact')!
}

async function header(t: Task): Promise<HTMLElement> {
  const ok = <T,>(data: T) => ({ status: 'ok' as const, data, fetchedAt: Date.now() })
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTask.mockResolvedValue(ok(t))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadResourceClasses.mockReturnValue(new Promise(() => {}))
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  render(<AgentSplit taskId={t.id} pane="attempts" artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${t.id}`} />)
  return waitFor(() => {
    const h = document.querySelector<HTMLElement>('.ag-head')
    expect(h?.querySelector('.ag-head-title')?.textContent).not.toBe(t.id)
    return h!
  })
}

/** The id line of `root`: the printed id and its copy button. */
function idLine(root: Element): { text: HTMLElement; copy: HTMLButtonElement; said: HTMLElement } {
  const line = root.matches('.tid') ? root : root.querySelector('.tid')
  expect(line, 'no task id line').not.toBeNull()
  return {
    text: line!.querySelector<HTMLElement>('.tid-text')!,
    copy: line!.querySelector<HTMLButtonElement>('button.tid-copy')!,
    said: line!.querySelector<HTMLElement>('[role="status"]')!,
  }
}

function clipboard(writeText: unknown): void {
  Object.defineProperty(navigator, 'clipboard', { value: writeText === undefined ? undefined : { writeText }, configurable: true })
}

afterEach(() => {
  vi.clearAllMocks()
  delete (navigator as unknown as { clipboard?: unknown }).clipboard
  window.getSelection()?.removeAllRanges()
})

describe('#94: the id is printed under the name, without a hover', () => {
  it.each([
    ['a workflow step', STEP],
    ['a standalone task', LONE],
  ])('prints the whole id, in mono, on a list row of %s', (_label, t) => {
    const r = row(t)
    const { text } = idLine(r.querySelector('.cr-name')!)
    // Its text, not a title: nothing has to be hovered to read it.
    expect(text.textContent).toBe(t.id)
    for (const env of [WIDE, PHONE]) {
      for (let el: Element | null = text; el !== null && el !== r; el = el.parentElement) {
        expect(painted(el, 'display', env), `hidden at ${env.width}`).not.toBe('none')
      }
    }
    // UNDER the name, in the name's cell.
    const name = r.querySelector('.cr-name b')!
    expect(name.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // DOM order holds beside the name too: the cell must STACK, or the id is
    // drawn on the name's line (`.row .agent` is a one-line flex row).
    const cell = r.querySelector('.cr-name')!
    for (const env of [WIDE, PHONE]) {
      expect(painted(cell, 'display', env), `name cell at ${env.width}`).toBe('flex')
      expect(painted(cell, 'flex-direction', env), `name cell at ${env.width}`).toBe('column')
    }
    expect(painted(text, 'font-family', WIDE)).toBe('var(--mono)')
  })

  it('prints the whole id under the step name in the split header', async () => {
    const head = await header(STEP)
    expect(head.querySelector('.ag-head-title')?.textContent).toBe('scan-08')
    const line = head.querySelector('.ag-head-id')
    expect(line, 'no id line in the header').not.toBeNull()
    // Directly under the title row.
    expect(line!.previousElementSibling?.classList.contains('ag-head-row')).toBe(true)
    expect(idLine(line!).text.textContent).toBe(ID)
    expect(painted(idLine(line!).text, 'font-family', WIDE)).toBe('var(--mono)')
  })

  it('holds the 12px floor and draws with theme tokens, so light and dark both read', () => {
    const { text } = idLine(row(STEP))
    expect(painted(text, 'font-size', WIDE)).toBe('var(--t-micro)')
    expect(STYLES).toMatch(/--t-micro:\s*12px/)
    expect(painted(text, 'color', WIDE) ?? '').toMatch(/^var\(--/)
  })
})

describe('#94: the copy button copies the whole id', () => {
  it('copies the full id from a row, and does not open the agent', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    clipboard(writeText)
    const onOpen = vi.fn()
    const { copy, said } = idLine(row(STEP, onOpen))
    expect(copy.getAttribute('aria-label')).toBe(`Copy task id ${ID}`)
    fireEvent.click(copy)
    // Called inside the click handler, before any await: that is the user
    // activation a browser's clipboard asks for.
    expect(writeText).toHaveBeenCalledWith(ID)
    expect(onOpen).not.toHaveBeenCalled()
    await waitFor(() => expect(said.textContent).toBe('task id copied'))
  })

  it('does not open the agent when Enter is pressed on the copy button', () => {
    clipboard(vi.fn().mockResolvedValue(undefined))
    const onOpen = vi.fn()
    const { copy } = idLine(row(STEP, onOpen))
    fireEvent.keyDown(copy, { key: 'Enter' })
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('copies the full id from the split header', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    clipboard(writeText)
    const head = await header(STEP)
    const { copy, said } = idLine(head.querySelector('.ag-head-id')!)
    fireEvent.click(copy)
    expect(writeText).toHaveBeenCalledWith(ID)
    await waitFor(() => expect(said.textContent).toBe('task id copied'))
  })
})

describe('#94: a refused clipboard selects the id instead', () => {
  it('selects the whole id when writeText rejects', async () => {
    clipboard(vi.fn().mockRejectedValue(new Error('NotAllowedError')))
    const { copy, said, text } = idLine(row(STEP))
    fireEvent.click(copy)
    await waitFor(() => expect(said.textContent).toMatch(/selected/))
    expect(window.getSelection()?.toString()).toBe(ID)
    expect(text.contains(window.getSelection()!.getRangeAt(0).commonAncestorContainer)).toBe(true)
  })

  it('selects the whole id when there is no clipboard (an insecure context)', () => {
    clipboard(undefined)
    const { copy, said } = idLine(row(LONE))
    fireEvent.click(copy)
    expect(window.getSelection()?.toString()).toBe(LONE.id)
    expect(said.textContent).toMatch(/selected/)
  })
})

describe('#94: at phone width the id stays on one line, cut in the middle', () => {
  it('keeps it on one line at 390, with both ends of the id drawn', () => {
    const r = row(STEP)
    const { text } = idLine(r)
    expect(painted(text, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(text, 'display', PHONE)).toMatch(/flex/)
    const head = text.querySelector('.tid-head')!
    const tail = text.querySelector('.tid-tail')!
    // The head gives way with an ellipsis; the tail never shrinks, so the id
    // is cut in the MIDDLE and its distinguishing end stays on screen.
    expect(painted(head, 'text-overflow', PHONE)).toBe('ellipsis')
    expect(painted(head, 'overflow', PHONE)).toBe('hidden')
    expect(painted(head, 'min-width', PHONE)).toBe('0')
    expect(painted(tail, 'flex', PHONE)).toBe('none')
    expect((head.textContent ?? '') + (tail.textContent ?? '')).toBe(ID)
    expect(tail.textContent!.length).toBeGreaterThanOrEqual(4)
    // The line itself cannot push the row wider than the phone.
    expect(painted(text, 'min-width', PHONE)).toBe('0')
  })

  it('copies the whole id at phone width, not the truncated text', () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    clipboard(writeText)
    fireEvent.click(idLine(row(STEP)).copy)
    expect(writeText).toHaveBeenCalledWith(ID)
    expect(writeText.mock.calls[0]![0]).not.toContain('…')
  })
})
