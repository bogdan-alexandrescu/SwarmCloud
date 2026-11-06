// THE CONSOLE CALLS THE RUNNING UNIT AN AGENT (VOCAB-AGENTS-UI, part of #1680).
//
// The owner's rule, 2026-10-20: the console says "agent" for the running unit
// everywhere; "task" stays ONLY in identifiers -- `task_…` ids, API routes
// such as /v1/tasks, Firestore collection names and code symbols. VOCAB-AGENTS
// (#1725) did the cards, counts and links and left six spots, which the owner
// answered the same day: the workflow cost and output facts, the step chips,
// the two Submit pages' counts, a pool's refusal title, the help topics' prose
// and the page titles and kind labels. Each case below is one of them.
//
// The route stays: the address /submit/task is a saved link, and only the
// words on the page change.
//
// Where a phrase is drawn by a component that is cheap to render, the case
// renders it. The workflow page's cost and output facts and its step titles
// sit in unexported helpers several screens deep, so those cases read the
// shipped source with its comments stripped: the phrase is a literal there,
// and a literal is what a revert would put back.

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import WORKFLOWS from '../Workflows.tsx?raw'
import WORKFLOW_VIEWS from '../WorkflowViews.tsx?raw'
import CAPACITY from '../Capacity.tsx?raw'
import SUBMIT_CHOOSER from '../SubmitChooser.tsx?raw'
import { SECTIONS } from '../App'
import { HELP } from '../help'
import { agentName } from '../agentlist'
import { StepMark, type StepRow } from '../RunSteps'
import { ProfileFacts } from '../Submit'
import type { Pool, RunnerProfile } from '../types'

/** The source without its comments, so a comment quoting the old copy does not count. */
function code(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((l) => !l.trim().startsWith('//'))
    .join('\n')
}

/**
 * "task" as a word of prose: not inside an identifier, a path or a class name
 * (`task_…`, `/v1/tasks`, `data-task-id`, `strategy-on-a-task`), and not the
 * name of the id itself ("task id"), which is what the id is called.
 */
const PROSE_TASK = /(?<![/_\w-])[Tt]asks?(?![/_\w:-])(?! ids?\b)/

function strings(v: unknown): string[] {
  if (typeof v === 'string') return [v]
  if (Array.isArray(v)) return v.flatMap(strings)
  if (v !== null && typeof v === 'object') return Object.values(v).flatMap(strings)
  return []
}

describe('6. page titles and kind labels say agent', () => {
  it('names the Submit form "Start an agent" in the nav, and keeps its address', () => {
    const submit = SECTIONS.flatMap((s) => s.tabs).find((t) => t.id === 'new')
    // MUTATION: put `label: 'Submit a task'` back on the `new` tab in App.tsx's
    // SECTIONS and this goes red.
    expect(submit?.label).toBe('Start an agent')
  })

  it('heads a lone agent "<profile> agent · <short id>"', () => {
    // MUTATION: put `${profile} task` back in agentName (agentlist.ts) and this
    // goes red: the header read "claude-code task · 01234567".
    expect(agentName({ id: 'task_0123456789abcdef0123', runner_profile: 'claude-code', step_id: null })).toBe(
      'claude-code agent · 01234567',
    )
  })
})

describe('2. a step chip with nothing created yet says "no agent yet"', () => {
  const row: StepRow = { key: '0:plan', round: 0, stepId: 'plan', title: null, taskId: null, task: null, dependsOn: [], merge: null }

  it('on the issue run Steps card', () => {
    const { container } = render(<StepMark row={row} />)
    // MUTATION: put 'no task yet' back as StepMark's word (RunSteps.tsx) and
    // this goes red.
    expect(container.textContent).toContain('no agent yet')
    expect(container.textContent).not.toMatch(PROSE_TASK)
  })

  it('on the workflow page and its inspector', () => {
    // MUTATION: put "This step has no task yet. The workflow has not reached
    // it." back as present()'s title in Workflows.tsx and this goes red.
    expect(code(WORKFLOWS)).toContain("'This step has no agent yet. The workflow has not reached it.'")
    expect(code(WORKFLOWS)).not.toContain('has no task yet')
    // MUTATION: put "This step has no task yet" back in StepInspector's
    // aria-label (WorkflowViews.tsx) and this goes red.
    expect(code(WORKFLOW_VIEWS)).not.toContain('has no task yet')
  })
})

describe('1. the workflow cost and output facts say "no agent joined"', () => {
  it('in the cost fact, the output fact and its short word', () => {
    const src = code(WORKFLOWS)
    // MUTATION: put "No task was joined for this workflow" back in Spend or
    // WorkflowDispatch (Workflows.tsx) and this goes red.
    expect(src).not.toContain('No task was joined')
    expect(src).toContain('No agent was joined for this workflow, so nothing could have reported a cost.')
    expect(src).toContain('No agent was joined for this workflow, so what it publishes cannot be read here.')
    // MUTATION: put 'no task joined' back as WorkflowDispatch's word and this goes red.
    expect(src).not.toContain("'no task joined'")
    expect(src).toContain("'no agent joined'")
  })
})

describe('3. the Submit pages count agents', () => {
  it('says "Room for N more agents" under the runner', () => {
    const pools: Pool[] = ['tenant:acme', 'global', 'resource:standard'].map((name) => ({
      name,
      hard_limit: 50,
      adaptive_target: null,
      quota_derived_limit: null,
      effective_limit: 50,
      active: 0,
      available: 50,
      enabled: true,
      updated_at: '2026-09-24T10:00:00Z',
    }))
    const profile: RunnerProfile = {
      resource_class: 'standard',
      backend: 'cloudrun',
      provider: null,
      units: 1,
      pools: pools.map((p) => p.name),
      admission: {
        units: 1, headroom: 3, basis: 'measured', blockers: [], binding: ['global'],
        counterfactual: [], complete: true, unread: [], uncapped: [],
      },
    }
    const { container } = render(<ProfileFacts name="claude-code" profile={profile} pools={pools} />)
    const line = Array.from(container.querySelectorAll('p')).find((p) => /Room for/.test(p.textContent ?? ''))
    // MUTATION: put "more task{...}" back in ProfileFacts (Submit.tsx) and this
    // goes red: it read "Room for 3 more tasks".
    expect(line?.textContent ?? '').toMatch(/Room for 3 more agents\b/)
    expect(line?.textContent ?? '').not.toMatch(PROSE_TASK)
  })

  it('says "your newest N agents" on the chooser', () => {
    // MUTATION: put `your newest ${w.read} task…` back in SubmitChooser.tsx
    // and this goes red.
    // The window is worded by `windowWords` (visual QA pass 2, runs2-10).
    expect(code(SUBMIT_CHOOSER)).toContain('`your newest ${w.read} agent${')
    expect(code(SUBMIT_CHOOSER)).not.toMatch(/your newest \$\{\w+(\.\w+)?\} task/)
  })
})

describe('4. a pool too small for one agent of its class says agent', () => {
  it('in the refusal title', () => {
    // MUTATION: put "units one ${weight?.name} task weighs, so it admits no
    // ${weight?.name} task" back in classifyPool (Capacity.tsx) and this goes red.
    expect(code(CAPACITY)).toContain('units one ${weight?.name} agent weighs, so it admits no ${weight?.name} agent at this limit')
    expect(code(CAPACITY)).not.toMatch(/\$\{weight\?\.name\} task/)
  })
})

describe('5. the help topics explain the mechanisms in agents and attempts', () => {
  it('no topic calls the running unit a task', () => {
    const offenders = Object.entries(HELP).flatMap(([id, topic]) =>
      strings(topic).filter((s) => PROSE_TASK.test(s)).map((s) => `${id}: ${s.slice(0, 120)}`),
    )
    // MUTATION: put "A task waiting for capacity has none" back in the
    // attempt-documents topic, or "every component that ends a task" in
    // failure-classes (help.ts), and this goes red naming the topic.
    expect(offenders).toEqual([])
  })

  it('keeps "attempt" for the attempt', () => {
    const text = strings(HELP['attempt-documents' as keyof typeof HELP]).join(' ')
    // MUTATION: rename the attempt as well ("An agent document is written…")
    // and this goes red: an attempt is not an agent.
    expect(text).toContain('An attempt document is written when capacity is reserved, not when an agent is submitted.')
  })
})
