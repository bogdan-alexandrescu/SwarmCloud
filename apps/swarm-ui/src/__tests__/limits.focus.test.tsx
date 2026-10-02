// POOL LIMITS: THE SIDE EDITOR'S KEYBOARD CONTRACT.
//
// The editor unmounts with the focused control inside it. Without a hand-back,
// Cancel or a saved write dropped a keyboard reader to the top of the page.
// Closing returns focus to the pool row's Edit control; Escape closes the
// editor, except while a write is in flight.
//
// MUTATION: drop the refocus effect in AdminSettingsScreen (cases 1 and 2), or
// the `busy` guard in the editor's onKeyDown (case 4), or the handler (case 3).

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

beforeEach(() => {
  api.loadCapacity.mockReset()
  api.setPoolLimit.mockReset()
  api.loadMe.mockReset()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-01T10:00:00Z' }
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 4,
    available: 6,
    enabled: true,
    updated_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

function capacity(): Capacity {
  return {
    pools: [pool('global', { hard_limit: 40, effective_limit: 40, available: 36 }), pool('tenant:eng')],
    runner_profiles: {},
    tenant_id: 'eng',
    generated_at: '2026-10-01T10:00:00Z',
  }
}

async function open(name: string): Promise<{ side: HTMLElement; edit: HTMLElement }> {
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  render(<AdminSettingsScreen />)
  const row = await waitFor(() => {
    const r = document.getElementById(`limit-${name}`)
    expect(r).not.toBeNull()
    return r as HTMLElement
  }, WAIT)
  const edit = within(row).getByRole('button', { name: `Edit ceiling for ${name}` })
  fireEvent.click(edit)
  const side = document.querySelector<HTMLElement>('aside.adm-side')
  expect(side, 'edit opened no side editor').not.toBeNull()
  return { side: side!, edit }
}

describe('closing the side editor hands focus back to the row', () => {
  it('on Cancel, focuses the pool row\'s Edit control', async () => {
    const { side, edit } = await open('tenant:eng')
    fireEvent.click(within(side).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(document.querySelector('aside.adm-side')).toBeNull(), WAIT)
    expect(document.activeElement).toBe(edit)
  })

  it('on a saved write, focuses the pool row\'s Edit control', async () => {
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    const { side, edit } = await open('tenant:eng')
    fireEvent.change(within(side).getByLabelText('Hard limit for tenant:eng'), { target: { value: '20' } })
    fireEvent.click(within(side).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.setPoolLimit).toHaveBeenCalledWith('tenant:eng', 20), WAIT)
    await waitFor(() => expect(document.querySelector('aside.adm-side')).toBeNull(), WAIT)
    expect(document.activeElement).toBe(edit)
  })
})

describe('Escape in the side editor', () => {
  it('closes it and returns focus to the Edit control', async () => {
    const { side, edit } = await open('tenant:eng')
    fireEvent.keyDown(within(side).getByLabelText('Hard limit for tenant:eng'), { key: 'Escape' })
    await waitFor(() => expect(document.querySelector('aside.adm-side')).toBeNull(), WAIT)
    expect(document.activeElement).toBe(edit)
  })

  it('does nothing while a write is in flight', async () => {
    api.setPoolLimit.mockReturnValue(new Promise(() => {}))
    const { side } = await open('tenant:eng')
    const field = within(side).getByLabelText('Hard limit for tenant:eng')
    fireEvent.change(field, { target: { value: '20' } })
    fireEvent.click(within(side).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(side).getByRole('button', { name: 'Saving…' })).toBeTruthy(), WAIT)
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(document.querySelector('aside.adm-side'), 'Escape closed an editor with a write in flight').not.toBeNull()
  })
})
