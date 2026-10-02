// THE SPLIT'S SMALL RULES: which task gets a Children tab, and where the
// collapse toggle opens the list again.
//
// MUTATIONS, one per block: return `childrenServed(task)` from
// `offersChildren`; open the strip to 'list' whatever the reader had chosen.

import { afterEach, describe, expect, it } from 'vitest'

import { childrenServed, offersChildren } from '../AgentChildren'
import { listSnap, resetListSnap, setListSnap, toggleListSnap } from '../listSnap'
import { task } from './runfixture'

afterEach(() => {
  localStorage.removeItem('swarm.agents.list')
  resetListSnap()
})

describe('the Children tab', () => {
  it('is not offered by an API that does not serve the child fields', () => {
    const t = task()
    delete t.parent_task_id
    expect(offersChildren(t)).toBe(false)
  })

  it('is offered on a task that is not itself a child', () => {
    expect(offersChildren(task({ parent_task_id: null }))).toBe(true)
  })

  it('is not offered on a child, which at depth 1 can have no children', () => {
    const child = task({ parent_task_id: 'tsk_parent' })
    expect(childrenServed(child)).toBe(true)
    expect(offersChildren(child)).toBe(false)
  })
})

describe('the collapse toggle', () => {
  it('opens the strip again to the width it was folded from', () => {
    setListSnap('half')
    toggleListSnap()
    expect(listSnap()).toBe('strip')
    toggleListSnap()
    expect(listSnap()).toBe('half')
  })

  it('opens to the compact list when no other width was chosen', () => {
    setListSnap('strip')
    toggleListSnap()
    expect(listSnap()).toBe('list')
  })
})
