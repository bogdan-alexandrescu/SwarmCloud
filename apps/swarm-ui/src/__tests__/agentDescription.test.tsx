/**
 * A SINGLE TASK'S DESCRIPTION ON ITS AGENT PAGE (owner, 2026-11-07, part of
 * #2688). swarm-api serves `description` on a task's reads: the task's own,
 * null for a workflow's step. The agent page shows it in a details area under
 * the name, its first line by default with a control to expand it, drawn as
 * text, and nothing when there is none.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { task as runTask } from './runfixture'
import type { Task } from '../types'

const api = vi.hoisted(() => ({
  loadResourceClasses: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgHead } = await import('../agent-split/head')

const ID = 'task_0123456789abcdef0123'
const FIRST = 'Rebuilds the size chart from the catalogue.'
const REST = 'The chart reads one table, and the page shows it under the product name.'

function head(over: Partial<Task>) {
  const task = runTask({ id: ID, state: 'RUNNING', completed_at: null, metadata: { title: 'Size chart' }, ...over })
  return render(
    <AgHead taskId={ID} task={task} runs={1} read="ok" reload={() => {}} onClose={() => {}} live={{ status: 'idle' }} reloadLive={() => {}} />,
  )
}

describe('the agent page shows a single task’s own description', () => {
  it('draws its first line under the name, and the whole text once expanded', () => {
    const { container } = head({ description: `${FIRST}\n${REST}` })
    const area = container.querySelector('.ag-desc')
    expect(area).not.toBeNull()
    // Under the name: after the head's row, in the same head.
    expect(container.querySelector('.ag-head-row + .ag-desc')).toBe(area)
    const text = area!.querySelector('.ag-desc-text')!
    expect(text.textContent).toBe(FIRST)
    const toggle = screen.getByRole('button', { name: 'More' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(text.textContent).toBe(`${FIRST}\n${REST}`)
    expect(screen.getByRole('button', { name: 'Less' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('draws markup as the text it is', () => {
    const { container } = head({ description: 'Adds a <b>size</b> chart. See [it](https://example.com).' })
    const text = container.querySelector('.ag-desc-text')!
    expect(text.querySelector('b, a')).toBeNull()
    expect(text.textContent).toBe('Adds a <b>size</b> chart. See [it](https://example.com).')
  })

  it('draws nothing when there is none, and no control for one line that fits', () => {
    expect(head({ description: null }).container.querySelector('.ag-desc')).toBeNull()
    expect(head({ description: '   ' }).container.querySelector('.ag-desc')).toBeNull()
    const { container } = head({ description: FIRST })
    expect(container.querySelector('.ag-desc-text')!.textContent).toBe(FIRST)
    expect(container.querySelector('.ag-desc button')).toBeNull()
  })

  it('draws nothing for a workflow’s step, which inherits nothing', () => {
    const { container } = head({ description: FIRST, workflow_id: 'wf_1', step_id: 'build' })
    expect(container.querySelector('.ag-desc')).toBeNull()
  })

  it('adds no sentence of its own around the user’s text', () => {
    const { container } = head({ description: FIRST })
    expect(container.querySelector('.ag-desc')!.textContent).toBe(FIRST)
  })
})
