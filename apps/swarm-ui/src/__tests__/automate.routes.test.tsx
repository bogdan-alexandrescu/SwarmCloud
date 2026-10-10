// AUTOMATE'S ADDRESSES AND ITS PLACE ON THE SPINE (docs/schedules.md §6.1,
// lane S7).
//
//   * `/schedules`, `/schedules/<id>`, `/schedules/<id>/<tab>`,
//     `/schedules/<id>/edit`, `/schedules/new`, `/approvals`,
//     `/approvals/<id>` and `/admin/schedules` each round-trip through the
//     router's address, so a pasted link opens the page it names;
//   * `/automate` opens the section's first page;
//   * the spine reads Overview · Work · Automate · Capacity · Admin, and the
//     Automate item carries the pending count, with a failed read never drawn
//     as a 0.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { addressToPath, pathToAddress } from '../paths'
import { approvalsCount } from '../Spine'

afterEach(() => cleanup())

describe('the paths', () => {
  const pairs: [string, string][] = [
    ['automate/schedules', '/schedules'],
    ['automate/schedules?schedule=sch_0a1b2c3d4e5f', '/schedules/sch_0a1b2c3d4e5f'],
    ['automate/schedules?schedule=sch_0a1b2c3d4e5f&tab=gate', '/schedules/sch_0a1b2c3d4e5f/gate'],
    ['automate/schedules?schedule=sch_0a1b2c3d4e5f&page=edit', '/schedules/sch_0a1b2c3d4e5f/edit'],
    ['automate/schedules?page=new', '/schedules/new'],
    ['automate/approvals', '/approvals'],
    ['automate/approvals?item=apr_0a1b2c3d', '/approvals/apr_0a1b2c3d'],
    ['admin/schedules', '/admin/schedules'],
  ]
  it.each(pairs)('%s is %s, and back', (address, path) => {
    expect(addressToPath(address)).toBe(path)
    const [p, q] = path.split('?') as [string, string | undefined]
    expect(pathToAddress(p, q ?? '')?.address).toBe(address)
  })

  it('opens the first page at the section root', () => {
    expect(pathToAddress('/automate')?.address).toBe('automate/schedules')
  })
})

describe('the spine', () => {
  it('reads Overview · Work · Automate · Capacity · Admin', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    globalThis.fetch = vi.fn(async () => new Response('{}', { status: 404, headers: { 'content-type': 'application/json' } })) as unknown as typeof fetch
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="automate" tab="schedules" title="x" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    const items = Array.from(document.querySelectorAll('.sk-ri[data-sec]')).map((a) => a.getAttribute('data-sec'))
    expect(items).toEqual(['overview', 'work', 'automate', 'capacity', 'admin'])
    const pages = Array.from(document.querySelectorAll('.sk-panel .sk-pk .sk-pl')).map((p) => p.textContent)
    expect(pages).toEqual(['Schedules', 'Approvals'])
    vi.unstubAllEnvs()
  })

  it('counts pending items, and a failed read is a dash with its reason, never 0', () => {
    const pending = { approval_id: 'a', state: 'pending' } as never
    const done = { approval_id: 'b', state: 'approved' } as never
    expect(approvalsCount({ status: 'ok', fetchedAt: 0, data: { approvals: [pending, done] } })).toMatchObject({ n: 1, alert: true })
    expect(approvalsCount({ status: 'empty', fetchedAt: 0 })).toMatchObject({ n: 0 })
    const failed = approvalsCount({ status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } })
    expect(failed?.n).toBeNull()
    expect(failed?.why).toMatch(/^not read: /)
    expect(approvalsCount({ status: 'loading', since: 0 })).toBeUndefined()
  })
})
