// QA2-FIX-25 (part of #2694, console visual QA pass 2, and #2391). WHAT EACH CASE HOLDS:
//   * admin2-02: an Admin page a workspace admin is shown (People and roles, Workspaces you admin) is refused as
//     "for workspace admins", saying the reader administers no group workspace; and the owner of a personal
//     workspace is told it is their own workspace, never called its admin;
//   * cap2-01: a paused account cannot take a new agent, whatever the flag read before the pause says, so its
//     row reads No and the verdict's free slots drop by its slots;
//   * capacity-04: the Healthy tile's hint no longer says "nothing to do", and the healthy accounts the pool
//     withholds (skipped here, a second registration, a state someone set) are named.
//
// Fixtures only; no network.

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AdminRoleRefusal } from '../WorkspaceRefusal'
import { CanTakeChip } from '../AccountsTable'
import { loadAccountHealthBoard, loadAccountPool } from '../api'
import { canTake, groupOf, verdictOf, type PoolAccount } from '../accountsPool'
import {
  STATUS_HINT,
  healthyWithheld,
  healthyWithheldLine,
  type AccountHealthRecord,
  type HealthAccount,
} from '../accountHealthModel'
import { visible } from './repofixture'

afterEach(cleanup)

describe('admin2-02: a workspace-admin page refused to someone who administers no group workspace', () => {
  it('titles it for workspace admins and says the reader administers none', () => {
    const { container } = render(
      <AdminRoleRefusal page="Workspaces you admin" role="member" workspace="Engineering" audience="workspace" />,
    )
    // MUTATION: title every refusal "for platform admins" and this is not found.
    expect(screen.getByText('Workspaces you admin is for workspace admins')).toBeTruthy()
    // MUTATION: drop the workspace lead and this fails.
    expect(visible(container)).toContain('This page is for admins of a group workspace; you administer none.')
    expect(visible(container)).toContain('You are a member of Engineering.')
    expect(visible(container)).not.toContain('platform admin')
  })

  it('says a personal workspace is the reader’s own, never calling them its admin', () => {
    const { container } = render(
      <AdminRoleRefusal page="People and roles" role="admin" workspace="Bogdan" audience="workspace" personal />,
    )
    // MUTATION: ignore `personal` and the line reads "You are an admin of Bogdan." again.
    expect(visible(container)).toContain('Bogdan is your own workspace.')
    expect(visible(container)).not.toContain('an admin of')
  })

  it('keeps a platform-only page as it was', () => {
    const { container } = render(<AdminRoleRefusal page="Pool limits" role="admin" workspace="Bogdan" personal />)
    expect(screen.getByText('Pool limits is for platform admins')).toBeTruthy()
    expect(visible(container)).toContain('You are not a platform admin. Bogdan is your own workspace.')
  })
})

describe('cap2-01: a paused account cannot take a new agent', () => {
  async function pool(): Promise<{ accounts: PoolAccount[]; scope: string | null }> {
    const r = await loadAccountPool()
    if (r.status !== 'ok') throw new Error('the fixture accounts did not load')
    return { accounts: r.data.accounts as PoolAccount[], scope: r.data.tenant_id ?? null }
  }

  it('reads No once paused, though the flag read before the pause says yes', async () => {
    const { accounts, scope } = await pool()
    const primary = accounts.find((a) => a.account_id === 'u-bogdan:primary') as PoolAccount
    expect(canTake(primary, scope)).toBe(true)
    // The fixture's PUT /state sets the state and keeps the flag, as a read made before the pause does.
    const paused: PoolAccount = { ...primary, state: 'PAUSED', reason: 'holiday', can_take_new_agent: true }
    // MUTATION: drop the state check from canTake() and this is true.
    expect(canTake(paused, scope)).toBe(false)
    expect(groupOf(paused, scope)).toBe('person')
    const { container } = render(<CanTakeChip a={paused} scope={scope} />)
    expect(container.textContent).not.toMatch(/^Yes/)
  })

  it('drops its slots from the verdict', async () => {
    const { accounts, scope } = await pool()
    const before = verdictOf(accounts, scope, 0)
    const primary = accounts.find((a) => a.account_id === 'u-bogdan:primary') as PoolAccount
    const after = verdictOf(
      accounts.map((a) => (a === primary ? { ...a, state: 'PAUSED' as const } : a)),
      scope,
      0,
    )
    // MUTATION: drop the state check from canTake() and both stay as before.
    expect(after.n).toBe((before.n as number) - 1)
    expect(after.free).toBe(before.free - 4)
  })
})

describe('capacity-04: Healthy is not "nothing to do" over accounts the pool withholds', () => {
  it('words the hint as what Healthy means', () => {
    // MUTATION: restore 'nothing to do' and this fails.
    expect(STATUS_HINT.healthy).toBe('no limit, error or sign-in trouble')
  })

  it('names the healthy accounts the pool withholds on the development pool', async () => {
    const r = await loadAccountHealthBoard()
    if (r.status !== 'ok' || r.data.health.kind !== 'ok') throw new Error('the fixture board did not load')
    const accounts = r.data.page.accounts as HealthAccount[]
    const records = new Map<string, AccountHealthRecord>(r.data.health.pool.accounts.map((x) => [x.account_id, x]))
    const held = healthyWithheld(accounts, records, r.data.page.tenant_id ?? null).map((a) => a.account_id)
    // MUTATION: drop the skipped-here or the duplicate clause and one of these is missing.
    expect(held).toEqual(expect.arrayContaining(['eng:shared', 'u-bogdan:team-copy']))
    // A never-polled account takes its one agent; a full one frees when an agent finishes.
    expect(held).not.toContain('u-bogdan:never-polled')
    expect(held).not.toContain('u-bogdan:full-six')
    expect(healthyWithheldLine([])).toBeNull()
    expect(healthyWithheldLine(accounts.filter((a) => held.includes(a.account_id)))).toMatch(
      /^\d of them cannot take an agent: .*shared.*team-copy|^\d of them cannot take an agent: .*team-copy.*shared/,
    )
  })
})
