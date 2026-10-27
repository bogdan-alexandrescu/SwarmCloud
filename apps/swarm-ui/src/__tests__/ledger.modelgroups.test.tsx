// MODEL-UI (docs/design/model-per-step.md §4.4, §4.8, part of #1435): the
// ledger's Reliability card offers the route's `model` and `model_class`
// groups beside profile and person, because grouping by runner profile no
// longer separates tiers: every class on `claude-code` is one profile. The
// route serves both groups (MODEL-LEDGER); this is where a reader picks them.
//
// BREAK IT: drop `model` or `model_class` from the card's choices; print the
// route's "not recorded" model row as a link or a blank -- each case fails.

import { describe, expect, it } from 'vitest'
import { fireEvent, render, within } from '@testing-library/react'

import { ledgerFixture } from '../outcomes.fixture'
import type { GroupBy } from '../outcomes'

const { ReliabilityCard } = await import('../Ledger')

function choices(root: HTMLElement): string[] {
  const group = within(root).getByRole('group', { name: 'Group by' })
  return within(group).getAllByRole('button').map((b) => b.textContent ?? '')
}

describe('Reliability groups by model and by model class', () => {
  it('offers both, for a tenant and on the platform view', () => {
    const tenant = render(<ReliabilityCard data={ledgerFixture()} group="runner_profile" platform={false} onGroup={() => {}} />)
    expect(choices(tenant.container)).toEqual(['runner profile', 'person', 'model', 'model class'])
    tenant.unmount()
    const platform = render(<ReliabilityCard data={ledgerFixture()} group="runner_profile" platform onGroup={() => {}} />)
    expect(choices(platform.container)).toEqual(['runner profile', 'workspace', 'person', 'model', 'model class'])
  })

  it('asks for the group it names', () => {
    const picked: GroupBy[] = []
    const { container } = render(
      <ReliabilityCard data={ledgerFixture()} group="runner_profile" platform={false} onGroup={(g) => picked.push(g)} />,
    )
    const group = within(container).getByRole('group', { name: 'Group by' })
    fireEvent.click(within(group).getByRole('button', { name: 'model class' }))
    fireEvent.click(within(group).getByRole('button', { name: 'model' }))
    expect(picked).toEqual(['model_class', 'model'])
  })

  it('heads the table by model, and prints the route\'s "not recorded" row as words', () => {
    const d = ledgerFixture()
    const row = d.groups.rows[0]!
    d.groups = {
      ...d.groups,
      by: 'model',
      rows: [{ ...row, key: 'claude-haiku-5-5', submitted: null }, { ...row, key: 'not recorded', submitted: null }],
      rows_total: 2,
    }
    const { container } = render(
      <ReliabilityCard data={d} group="model" platform={false} onGroup={() => {}}
        rowLink={() => ({ href: '#x', open: () => {} })} />,
    )
    expect(container.querySelector('thead th')?.textContent).toBe('Model')
    const heads = [...container.querySelectorAll<HTMLElement>('tbody th')]
    expect(heads[0]!.textContent).toContain('claude-haiku-5-5')
    expect(heads[1]!.querySelector('a'), 'a row nothing can filter on drawn as a link').toBeNull()
    expect(heads[1]!.querySelector('.ctl-em')?.textContent).toBe('not recorded')
  })
})
