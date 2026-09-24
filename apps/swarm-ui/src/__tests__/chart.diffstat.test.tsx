// The diverging diffstat, read back off the bars it drew.
//
// THE DEFECTS THESE EXIST FOR (redesign-v2 Panel 2 and §4 row 7):
//
//   * a binary-only commit drawn as 0/0 -- the measured-zero ring says
//     "changed nothing", which is exactly what `binary_files` exists to stop;
//   * a count that is not a number drawn as zero lines;
//   * deletions and insertions not sharing one zero, so a lopsided change does
//     not look lopsided;
//   * a harvested window of commits read as all of them.

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DiffstatChart } from '../charts/Diffstat'

const commits: unknown[] = [
  { sha: 'aaaaaaaaaaaa', subject: 'lines', insertions: 30, deletions: 12, files_changed: 3, binary_files: 0 },
  { sha: 'bbbbbbbbbbbb', subject: 'a logo', insertions: 0, deletions: 0, files_changed: 1, binary_files: 2 },
  { sha: 'cccccccccccc', subject: 'empty', insertions: 0, deletions: 0, files_changed: 0, binary_files: 0 },
  // An untyped Firestore dict can carry anything; this one carries a string.
  { sha: 'dddddddddddd', subject: 'broken', insertions: 'many', deletions: null, files_changed: 1, binary_files: 0 },
]

function row(container: HTMLElement, sha: string): Element {
  const r = container.querySelector(`[data-testid="diff-row"][data-sha="${sha}"]`)
  expect(r, `no row for ${sha}`).not.toBeNull()
  return r!
}

describe('lines changed per commit', () => {
  it('draws deletions left of one zero and insertions right of it', () => {
    const { container } = render(<DiffstatChart commits={commits} commitCount={4} />)
    const zeroX = Number(container.querySelector('line.ctl-chart-zeroline')!.getAttribute('x1'))
    const r = row(container, 'aaaaaaaaaaaa')
    const del = r.querySelector('[data-testid="diff-del"]')!
    const ins = r.querySelector('[data-testid="diff-ins"]')!
    const delEnd = Number(del.getAttribute('x')) + Number(del.getAttribute('width'))
    expect(delEnd).toBeCloseTo(zeroX - 1, 6)
    expect(Number(ins.getAttribute('x'))).toBeCloseTo(zeroX + 1, 6)
    // One scale: 30 insertions are drawn 2.5 times 12 deletions, give or
    // take the 1px gap either side of zero.
    const insW = Number(ins.getAttribute('width')) + 1
    const delW = Number(del.getAttribute('width')) + 1
    expect(insW / delW).toBeCloseTo(2.5, 6)
  })

  it('never draws a binary-only commit as a commit that changed nothing', () => {
    const { container } = render(<DiffstatChart commits={commits} commitCount={4} />)
    const r = row(container, 'bbbbbbbbbbbb')
    expect(r.getAttribute('data-kind')).toBe('binary-only')
    expect(r.querySelector('[data-testid="diff-zero"]'), 'a binary change wears the zero mark').toBeNull()
    expect(r.querySelector('[data-testid="diff-binary"]')).not.toBeNull()
    expect(container.querySelector('.ctl-chart-legend')?.textContent).toContain('binary files')
  })

  it('marks a commit that genuinely changed no lines as a measured zero', () => {
    const { container } = render(<DiffstatChart commits={commits} commitCount={4} />)
    const r = row(container, 'cccccccccccc')
    expect(r.getAttribute('data-kind')).toBe('zero')
    expect(r.querySelector('[data-testid="diff-zero"]')).not.toBeNull()
  })

  it('hatches a commit whose counts are not numbers, and draws no bar or zero for it', () => {
    const { container } = render(<DiffstatChart commits={commits} commitCount={4} />)
    const r = row(container, 'dddddddddddd')
    expect(r.getAttribute('data-kind')).toBe('absent')
    expect(r.querySelector('[data-testid="diff-ins"], [data-testid="diff-del"], [data-testid="diff-zero"]')).toBeNull()
    expect(r.querySelector('rect.ctl-chart-absent')?.getAttribute('fill') ?? '').toMatch(/^url\(#ctl-hatch-/)
  })

  it('says it is a window when fewer commits were harvested than exist', () => {
    const { container } = render(<DiffstatChart commits={commits.slice(0, 2)} commitCount={7} />)
    const cap = container.querySelector('[data-testid="diff-window"]')
    expect(cap?.textContent).toBe('newest 2 of 7')
    const whole = render(<DiffstatChart commits={commits} commitCount={4} />)
    expect(whole.container.querySelector('[data-testid="diff-window"]')).toBeNull()
  })
})
