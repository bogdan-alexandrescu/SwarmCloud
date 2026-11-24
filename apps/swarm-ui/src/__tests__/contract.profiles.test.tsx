// THE SUBMIT SCREENS OFFER WHAT THE API DECLARES, READ FROM THE GENERATED FILE
// (part of #72).
//
// `generated/constants/profiles.json` is written by scripts/gen_api_types.py
// from the live Python objects (scripts/api_constants/profiles.py): each
// runner profile's declared input keys, required keys, closed choices,
// worker_action and control_plane; the browser action shapes; and the
// dispatch strategies, carriers, defaults and refusal details swarm-api's
// validation uses. `--check` (CI, and the worker's pre-publish gate) fails
// while the file is stale, so comparing the console with it compares the
// console with Python -- without any test reading the other language's source.
//
// WHAT EACH CASE HOLDS, BOTH WAYS:
//   * every catalogue profile has a SUGGESTED entry and every entry names a
//     catalogue profile; a worker action or control-plane profile is offered
//     nothing;
//   * a runner profile is offered no key its contract does not declare, and
//     every key it declares is offered (SUGGESTED or a picker), set by the
//     platform's own runs, or listed below as a typed-only setting;
//   * generic's `command` picker is the declared choices, exactly;
//   * every browser action the form builds sends only fields its declared
//     shape takes, and every field the shape requires;
//   * the MCP-server and browser-tool pickers read a key a profile declares
//     and a field the API serves;
//   * the strategies the dispatch control draws, and the carriers and
//     defaults the console sends, are the ones validation accepts, and every
//     strategy validation can run is drawn;
//   * the console's TaskDispatch has exactly the keys codec.dispatch_of serves;
//   * each dispatch refusal the API sends is attributed to its field;
//   * every fixture task's dispatch is one the API could have sent.
//
// MUTATION: add `{ name: 'model', ... }` to SUGGESTED['claude-code'], drop
// `paths` from SUGGESTED.generic, add 'patches' to DISPATCH_CARRIERS, or draw
// a strategy validation does not accept, and a case here fails.

import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// The refusal cases hand postTask real 422s, so the fixture build's answer to
// the submit is turned off here; the fixture case reads the actual module.
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), USE_FIXTURES: false }))

import { AUTO, BROWSER_TOOL_KEY, declaredBrowserTools } from '../BrowserToolPicker'
import { DispatchChoice, type DispatchDraft } from '../Dispatch'
import generated from '../generated/constants/profiles.json'
import { MCP_SERVERS_KEY, declaredMcpServers } from '../McpServers'
import { QA_IDENTITY_KEY } from '../QaIdentityGrants'
import { ACTION_SHAPE, SUGGESTED, postTask } from '../Submit'
import {
  DEFAULT_CARRIER,
  DEFAULT_STRATEGY,
  DISPATCH_CARRIERS,
  DISPATCH_STRATEGIES,
  dispatchOf,
  type RunnerProfile,
  type Task,
  type TaskDispatch,
} from '../types'

interface GeneratedProfile {
  declared_inputs: string[]
  required_keys: string[]
  input_choices: Record<string, string[]>
  worker_action: string | null
  control_plane: boolean
}

const PROFILES = generated.profiles as unknown as Record<string, GeneratedProfile>
const ACTIONS = generated.browser_actions as unknown as Record<string, Record<string, boolean>>
const DISPATCH = generated.dispatch as unknown as {
  strategies: string[]
  dispatchable_strategies: string[]
  carriers: string[]
  default_strategy: string
  default_carrier: string
  task_dispatch_keys: string[]
  refusals: Record<'strategy' | 'carrier' | 'repository_url', Record<string, unknown>>
}

const sorted = (xs: Iterable<string>): string[] => [...xs].sort()

/** Profiles that start a runner: a worker action and a control-plane profile run none. */
const RUNNER_STARTING = Object.entries(PROFILES)
  .filter(([, p]) => p.worker_action === null && !p.control_plane)
  .map(([name]) => name)

/** Keys a dedicated picker on the task form offers, not SUGGESTED. */
const PICKED = new Set<string>([MCP_SERVERS_KEY, BROWSER_TOOL_KEY, QA_IDENTITY_KEY])

/** Keys the platform's own runs write (an issue run's `issue`, a review's
 *  `pull_request`), never typed on this form. */
const SET_BY_THE_PLATFORM = new Set<string>(['issue', 'pull_request'])

/** Declared keys the form leaves to "a setting of your own", deliberately:
 *  the mock's failure-path knobs, generic's output and grace ceilings, and the
 *  browser runner's rarer switches. A key declared later fails the "every
 *  declared key" case until it is offered or listed here. */
const TYPED_ONLY: Record<string, readonly string[]> = {
  mock: ['fail_message', 'exit_code', 'artifact_name', 'quota_exhausted', 'retry_after_seconds', 'artifact_before_park'],
  generic: ['grace_seconds', 'max_stdout_bytes', 'max_stderr_bytes'],
  browser: ['launch_timeout_ms', 'user_agent', 'extract_text', 'screenshot'],
}

function suggested(name: string): string[] {
  return (SUGGESTED[name] ?? []).map((s) => s.name)
}

describe('the generated file is read', () => {
  it('names profiles of every kind, and dispatch values', () => {
    expect(RUNNER_STARTING.length).toBeGreaterThan(5)
    expect(Object.values(PROFILES).some((p) => p.worker_action !== null)).toBe(true)
    expect(Object.values(PROFILES).some((p) => p.control_plane)).toBe(true)
    expect(DISPATCH.strategies.length).toBeGreaterThan(0)
    expect(DISPATCH.carriers.length).toBeGreaterThan(0)
  })
})

describe('the Submit form offers each profile what its contract declares', () => {
  it('has an entry for every catalogue profile and for nothing else', () => {
    expect(sorted(Object.keys(SUGGESTED))).toEqual(sorted(Object.keys(PROFILES)))
    // Contract request 67's profile, named: check-contract-parity.sh reads it here too.
    expect(SUGGESTED['claude-code-browser']).toBeDefined()
  })

  it('offers a worker action or a control-plane profile nothing', () => {
    for (const [name, p] of Object.entries(PROFILES)) {
      if (p.worker_action !== null || p.control_plane) expect(suggested(name), name).toEqual([])
    }
  })

  it.each(RUNNER_STARTING)('offers %s only keys it declares', (name) => {
    const offered = suggested(name)
    expect(offered.length, `the form offers ${name} nothing`).toBeGreaterThan(0)
    // The prompt is every profile's (validation.PLATFORM_INPUT_KEYS), declared or not.
    const declared = new Set([...generated.platform_input_keys, ...PROFILES[name]!.required_keys, ...PROFILES[name]!.declared_inputs])
    // An undeclared key is a submission the API refuses with 422 invalid_input.
    expect(offered.filter((k) => !declared.has(k)), name).toEqual([])
  })

  it.each(RUNNER_STARTING)('offers %s every key it declares, or says why not', (name) => {
    const p = PROFILES[name]!
    const typedOnly = TYPED_ONLY[name] ?? []
    // A listed key is declared and is not offered as well: neither list may go stale.
    expect(typedOnly.filter((k) => !p.declared_inputs.includes(k)), `${name}: listed but not declared`).toEqual([])
    expect(typedOnly.filter((k) => suggested(name).includes(k)), `${name}: listed and offered`).toEqual([])
    const reached = new Set([...suggested(name), ...typedOnly, ...PICKED, ...SET_BY_THE_PLATFORM])
    const missed = [...p.required_keys, ...p.declared_inputs].filter((k) => !reached.has(k))
    expect(missed, `${name} declares keys the form never offers`).toEqual([])
  })

  it("offers generic's command as exactly the declared choices", () => {
    const command = (SUGGESTED.generic ?? []).find((s) => s.name === 'command')
    expect(command?.choices).toBeDefined()
    expect(sorted(command?.choices ?? [])).toEqual(sorted(PROFILES.generic!.input_choices.command ?? []))
  })
})

describe('the browser actions the form builds are the declared shapes', () => {
  it('builds every declared action type and no other', () => {
    expect(sorted(Object.keys(ACTION_SHAPE))).toEqual(sorted(Object.keys(ACTIONS)))
  })

  it.each(Object.keys(ACTIONS))('sends a %s action only fields its shape takes, and every one it requires', (kind) => {
    const sent = (ACTION_SHAPE[kind] ?? []).map((part) => part.send ?? part.prop)
    const shape = ACTIONS[kind]!
    expect(sent.filter((f) => !(f in shape)), kind).toEqual([])
    const required = Object.entries(shape).filter(([, r]) => r).map(([f]) => f)
    expect(required.filter((f) => !sent.includes(f)), kind).toEqual([])
  })
})

describe('the input pickers read what the API serves', () => {
  const declaring = (key: string): string[] =>
    Object.entries(PROFILES).filter(([, p]) => p.declared_inputs.includes(key)).map(([n]) => n)

  /** A profile whose served declaration of `key` carries only the fields the API serves. */
  function serving(key: string, values: Record<string, unknown>): RunnerProfile {
    const decl = Object.fromEntries(generated.declaration_fields.filter((f) => f in values).map((f) => [f, values[f]]))
    return { input_contract: { required_keys: [], inputs: { [key]: decl } } } as unknown as RunnerProfile
  }

  it('draws the MCP-server picker for a declaring profile, from the served maximum', () => {
    expect(declaring(MCP_SERVERS_KEY).length).toBeGreaterThan(0)
    expect(declaredMcpServers(serving(MCP_SERVERS_KEY, { kind: 'list', maximum: 3 }))).toBe(3)
  })

  it('draws the browser-tool picker from the served choices', () => {
    const [name] = declaring(BROWSER_TOOL_KEY)
    expect(name).toBeDefined()
    const choices = PROFILES[name!]!.input_choices[BROWSER_TOOL_KEY] ?? []
    expect(choices.length).toBeGreaterThan(0)
    expect(declaredBrowserTools(serving(BROWSER_TOOL_KEY, { kind: 'string', choices }))).toEqual([
      AUTO, ...new Set(choices.filter((c) => c !== AUTO)),
    ])
  })
})

describe('the dispatch choices are the ones validation accepts', () => {
  function drawnStrategies(scale: 'task' | 'workflow'): string[] {
    const draft: DispatchDraft = { strategy: DEFAULT_STRATEGY, carrier: DEFAULT_CARRIER, repositoryUrl: '' }
    const { container, unmount } = render(
      <DispatchChoice draft={draft} onChange={() => undefined} steps={scale === 'task' ? 1 : 3} scale={scale} />,
    )
    const values = [...container.querySelectorAll<HTMLInputElement>('input[name="dispatch-strategy"]')].map((i) => i.value)
    unmount()
    return values
  }

  it.each(['task', 'workflow'] as const)('draws at %s scale only strategies validation accepts', (scale) => {
    const drawn = drawnStrategies(scale)
    expect(drawn.length).toBeGreaterThan(0)
    expect(drawn.filter((s) => !DISPATCH.strategies.includes(s))).toEqual([])
  })

  it('draws every strategy validation can run, on the workflow form', () => {
    expect(sorted(drawnStrategies('workflow'))).toEqual(sorted(DISPATCH.dispatchable_strategies))
    expect(sorted(DISPATCH_STRATEGIES)).toEqual(sorted(DISPATCH.dispatchable_strategies))
  })

  it('knows exactly the carriers validation accepts', () => {
    // `patches` is the WORKER's spelling and swarm-api refuses it.
    expect(sorted(DISPATCH_CARRIERS)).toEqual(sorted(DISPATCH.carriers))
  })

  it('pre-selects the API defaults', () => {
    expect(DEFAULT_STRATEGY).toBe(DISPATCH.default_strategy)
    expect(DEFAULT_CARRIER).toBe(DISPATCH.default_carrier)
  })

  it('reads a dispatch block into exactly the keys codec.dispatch_of serves', () => {
    // Compile-time: exactly TaskDispatch's keys, none missing, none extra.
    const declared: Record<keyof TaskDispatch, true> = { strategy: true, carrier: true, role: true, integrates: true }
    expect(sorted(Object.keys(declared))).toEqual(sorted(DISPATCH.task_dispatch_keys))
    const served = { strategy: DISPATCH.default_strategy, carrier: DISPATCH.default_carrier, role: null, integrates: [] }
    expect(sorted(Object.keys(served))).toEqual(sorted(DISPATCH.task_dispatch_keys))
    const read = dispatchOf({ dispatch: served } as unknown as Task)
    expect(read).not.toBeNull()
    expect(sorted(Object.keys(read ?? {}))).toEqual(sorted(DISPATCH.task_dispatch_keys))
  })
})

describe('a dispatch refusal is attributed to the field it is about', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it.each(['strategy', 'carrier', 'repository_url'] as const)('pins the %s refusal on %s', async (field) => {
    const detail = DISPATCH.refusals[field]
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ code: 'invalid_dispatch', message: `refused: ${field}`, detail }),
      { status: 422, headers: { 'content-type': 'application/json' } },
    )))
    const out = await postTask({ runner_profile: 'mock', input: {} })
    expect(out.kind).toBe('refused')
    if (out.kind !== 'refused') return
    expect(out.fields[field]).toBe(`refused: ${field}`)
    expect(out.unattributed).toBeNull()
  })
})

describe('the fixture build serves the catalogue', () => {
  // Each fixture contract's own content is held by its own test; this holds the names.
  it("serves only catalogue profiles, each with an input contract, contract request 67's among them", async () => {
    const actual = await vi.importActual<typeof import('../api')>('../api')
    expect(actual.USE_FIXTURES).toBe(true)
    const capacity = await actual.loadCapacity()
    expect(capacity.status).toBe('ok')
    const served = capacity.status === 'ok' ? capacity.data.runner_profiles : {}
    expect(Object.keys(served).filter((name) => !(name in PROFILES))).toEqual([])
    for (const [name, profile] of Object.entries(served)) expect(profile.input_contract, name).toBeDefined()
    expect(served['claude-code-browser']).toBeDefined()
  })
})

describe('the fixture build serves dispatch blocks the API could have sent', () => {
  it('carries accepted strategies and carriers, the default and integrate among them', async () => {
    const actual = await vi.importActual<typeof import('../api')>('../api')
    expect(actual.USE_FIXTURES).toBe(true)
    const page = await actual.loadTasks()
    expect(page.status).toBe('ok')
    const tasks = page.status === 'ok' ? page.data.tasks : []
    const blocks = tasks.map((t) => t.dispatch).filter((d): d is Record<string, unknown> => typeof d === 'object' && d !== null)
    expect(blocks.length).toBeGreaterThan(0)
    for (const d of blocks) {
      expect(sorted(Object.keys(d))).toEqual(sorted(DISPATCH.task_dispatch_keys))
      expect(DISPATCH.strategies).toContain(d.strategy)
      expect(DISPATCH.carriers).toContain(d.carrier)
    }
    expect(blocks.some((d) => d.strategy === DISPATCH.default_strategy)).toBe(true)
    expect(blocks.some((d) => d.strategy === 'integrate')).toBe(true)
  })
})
