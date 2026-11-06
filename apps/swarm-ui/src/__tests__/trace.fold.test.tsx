// A TRANSCRIPT IS READ FULL WIDTH (owner pick 2026-11-05, part of #2688):
// while the trace is drawn in the Work inspector the list beside it folds to
// a strip, one click brings it back, and it comes back by itself when the
// trace goes. (Beside the Agents list: trace.view.test.tsx.)
// MUTATIONS are named at each assertion.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { resetListSnap } from '../listSnap'
import { traceMounted } from '../trace/model'
import { Inspector } from '../work/Inspector'

function shell() {
  return render(
    <Inspector screen="trace-fold-test" main={<div>the list</div>} title="agent">
      <div>the agent</div>
    </Inspector>,
  )
}

const wi = () => document.querySelector<HTMLElement>('.wi')!

afterEach(() => resetListSnap())

describe('the Work inspector', () => {
  it('folds the list to a strip while a trace is drawn, and opens it again in one click', () => {
    shell()
    expect(wi().classList.contains('is-trwide')).toBe(false)
    let gone = () => {}
    act(() => {
      gone = traceMounted()
    })
    // Mutation: no `is-trwide` leaves the list beside a two-pane trace.
    expect(wi().classList.contains('is-trwide')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Show the list' }))
    expect(wi().classList.contains('is-trwide')).toBe(false)
    act(() => gone())
    expect(screen.queryByRole('button', { name: 'Show the list' })).toBeNull()
  })

  it('opens the list again when the trace goes', () => {
    shell()
    let gone = () => {}
    act(() => {
      gone = traceMounted()
    })
    act(() => gone())
    expect(wi().classList.contains('is-trwide')).toBe(false)
  })
})

describe('a later trace', () => {
  it('opens full width again after the reader brought the list back', () => {
    shell()
    let gone = () => {}
    act(() => {
      gone = traceMounted()
    })
    fireEvent.click(screen.getByRole('button', { name: 'Show the list' }))
    act(() => gone())
    act(() => {
      gone = traceMounted()
    })
    expect(wi().classList.contains('is-trwide')).toBe(true)
    act(() => gone())
  })
})
