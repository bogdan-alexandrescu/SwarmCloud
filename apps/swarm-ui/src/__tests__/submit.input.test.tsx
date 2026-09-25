// THE INPUT IS FIELDS NOW, AND THIS IS WHAT HOLDS IT THERE.
//
// WHAT MOVED. Both submit screens used to edit `input` as a
// `<textarea class="mono">` labelled `input (JSON object)`, seeded with the
// literal `{}` and run through `JSON.parse` on submit. The owner's verdict was
// that nobody should be pasting JSON objects into a form field, and the
// replacement is `InputFields` / `buildInput` in `src/Submit.tsx`: a named key,
// a kind, and a value editor per kind, with the object assembled in one place.
//
// THREE CLAIMS ARE WORTH A TEST AND THEY ARE THE THREE THIS FILE MAKES.
//
//   1. THE HONESTY INVARIANT SURVIVED THE RE-ENCODING. `required_keys` unread
//      and `required_keys` measured-empty are two different facts, they had two
//      different renderings on the old screen, and they still do. The old
//      encoding was a `.ctl-mark.is-unread` beside a textarea label; the new one
//      is the same mark above the field group. `describe('an unread rule is not
//      an absent rule')` is what goes red if anyone collapses them.
//
//   2. AN EMPTY VALUE IS OMITTED, NEVER COERCED. `JSON.parse` could not produce
//      this bug because a caller typed the whole object; typed fields can, and
//      the failure would be silent -- a blank `sleep_seconds` sent as `0` is a
//      different run from `sleep_seconds` absent, and nobody would ever see the
//      difference on screen. Same rule as every figure in this console: a
//      number nobody supplied is not zero.
//
//   3. NO JSON SURVIVES ANYWHERE IN EITHER FILE. Held as a source scan, for the
//      reason `typescale.test.ts` gives about uppercase: a rule on a code path
//      this suite never reaches is exactly where the next one survives.
//
// MUTATION-CHECKED, not merely green. Each of the three was confirmed by
// breaking the thing it guards -- rendering the unread mark unconditionally,
// making `buildInput` coerce `'' -> 0`, and re-adding a `JSON.parse` -- and
// each turned this file red with a named message.

import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import SUBMIT_SRC from '../Submit.tsx?raw'
import WORKFLOW_SRC from '../SubmitWorkflow.tsx?raw'
import {
  InputFields,
  SubmitScreen,
  buildInput,
  missingRequired,
  seedFields,
  type BrowserAction,
  type InputField,
  type ValueKind,
} from '../Submit'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'

let key = 100
function f(over: Partial<InputField> = {}): InputField {
  return {
    key: key++, name: '', kind: 'text' as ValueKind, text: '', flag: false,
    list: [''], actions: [], required: false, own: false, ...over,
  }
}
function act(over: Partial<BrowserAction> = {}): BrowserAction {
  return { key: key++, type: 'goto', url: '', selector: '', text: '', name: '', seconds: '1', ...over }
}
function built(fields: InputField[]): Record<string, unknown> {
  const out = buildInput(fields)
  if (!out.ok) throw new Error(`expected a built object, got a refusal: ${out.message}`)
  return out.input
}

describe('an empty value is omitted, never coerced', () => {
  it('a blank number sends no key at all -- not 0', () => {
    const input = built([f({ name: 'sleep_seconds', kind: 'number', text: '' })])
    // `'sleep_seconds' in input` and not `input.sleep_seconds === undefined`:
    // the claim is that the KEY is absent, and `{sleep_seconds: undefined}`
    // would satisfy the weaker test and serialise to `{}` anyway -- passing for
    // the wrong reason on the day someone writes it that way.
    expect('sleep_seconds' in input, 'a number nobody typed is not zero').toBe(false)
  })

  it('a blank text sends no key -- not the empty string the runner refuses', () => {
    // `run_cli_agent` tests `isinstance(prompt, str) and prompt.strip()`, so
    // `""` is dispatched, given a credential, and refused minutes later. It
    // must not leave this browser.
    const input = built([f({ name: 'prompt', text: '   ' })])
    expect('prompt' in input).toBe(false)
  })

  it('an unchecked flag DOES send false, because false is an answer', () => {
    // The distinction this whole file is about: an absence is omitted, a
    // measured negative is sent. `fail: false` is a caller saying "do not
    // fail"; a blank number is a caller saying nothing.
    expect(built([f({ name: 'fail', kind: 'flag', flag: false })])).toEqual({ fail: false })
  })

  it('an empty list sends no key, and a part-filled one drops the blanks', () => {
    expect('paths' in built([f({ name: 'paths', kind: 'list', list: ['', ' '] })])).toBe(false)
    expect(built([f({ name: 'paths', kind: 'list', list: ['tests/', '', 'src/'] })]))
      .toEqual({ paths: ['tests/', 'src/'] })
  })

  it('a number that is not a number is refused here, naming the key', () => {
    const out = buildInput([f({ name: 'steps', kind: 'number', text: '4four' })])
    expect(out.ok).toBe(false)
    expect(out.ok === false && out.message).toContain('steps')
  })

  it('the same key twice is refused: an object has one value per key', () => {
    const out = buildInput([f({ name: 'prompt', text: 'a' }), f({ name: 'prompt', text: 'b' })])
    expect(out.ok).toBe(false)
    expect(out.ok === false && out.message).toContain('prompt')
  })

  it('a key called __proto__ is sent as a key, not swallowed by the prototype', () => {
    // The caller types the key. On a plain `{}`, `input.__proto__ = 'x'` sets
    // the PROTOTYPE: the screen would list the setting, the duplicate check
    // would not see it, and the body would not carry it -- a field that is on
    // screen and not in the request is exactly the class of lie this app
    // exists to remove. `buildInput` builds on a null-prototype object.
    //
    // ASSERTED ON THE SERIALISED STRING, because `{ __proto__: 'x' }` on the
    // EXPECTED side has the identical problem -- an object literal with that
    // key sets the prototype, so the naive expectation is `{}` and this test
    // would fail against correct code. The request body is a string; compare
    // the string.
    const out = buildInput([f({ name: '__proto__', text: 'x' })])
    expect(out.ok).toBe(true)
    expect(JSON.stringify(out.ok === true ? out.input : null)).toBe('{"__proto__":"x"}')
  })

  it('an unnamed field is skipped rather than sent under an empty key', () => {
    expect(built([f({ name: '', text: 'orphan' }), f({ name: 'prompt', text: 'go' })]))
      .toEqual({ prompt: 'go' })
  })
})

describe('browser actions are built, not typed', () => {
  it('each type sends exactly the scalars its runner branch reads', () => {
    // `runners/browser.py:112-157`. `fill` reads selector + text; `wait` reads
    // seconds AS A NUMBER; `goto` reads url. Nothing else is sent for any of
    // them, so a leftover value from a type the caller flipped away from
    // cannot ride along.
    const input = built([f({
      name: 'actions', kind: 'actions',
      actions: [
        act({ type: 'goto', url: 'https://example.test', selector: 'left over' }),
        act({ type: 'fill', selector: '#q', text: 'hello' }),
        act({ type: 'wait', seconds: '2.5' }),
      ],
    })])
    expect(input.actions).toEqual([
      { type: 'goto', url: 'https://example.test' },
      { type: 'fill', selector: '#q', text: 'hello' },
      { type: 'wait', seconds: 2.5 },
    ])
  })

  it('an action missing the part its type needs is refused before it is sent', () => {
    const out = buildInput([f({ name: 'actions', kind: 'actions', actions: [act({ type: 'click', selector: '' })] })])
    expect(out.ok).toBe(false)
    expect(out.ok === false && out.message).toContain('selector')
  })

  it('no actions at all sends no key, so the runner reads its own default', () => {
    expect('actions' in built([f({ name: 'actions', kind: 'actions', actions: [] })])).toBe(false)
  })
})

describe('an unread rule is not an absent rule', () => {
  const render0 = (required: string[] | null, fields: InputField[] = []) =>
    render(<InputFields profile="claude-code" fields={fields} required={required} idPrefix="t" onChange={() => {}} />)

  it('null renders the unread mark; an empty list does not', () => {
    // THE WHOLE CLAIM, IN ONE ASSERTION PAIR. `null` is "this API did not say
    // which keys the runner demands"; `[]` is "it said, and there are none".
    // A screen that drew them the same would be reporting a measurement it
    // never took.
    const unread = render0(null)
    expect(unread.container.querySelector('.ctl-mark.is-unread')).not.toBeNull()
    unread.unmount()

    const measured = render0([])
    expect(
      measured.container.querySelector('.ctl-mark.is-unread'),
      'a measured "nothing is required" must not borrow the unread mark',
    ).toBeNull()
  })

  it('the unread case keeps an accessible route to the words', () => {
    // §13.5's rule: the surface may carry the mark alone, but the sentence
    // that says what the mark MEANS has to stay reachable.
    const label = render0(null).container.querySelector('.ctl-panel-note')?.getAttribute('aria-label') ?? ''
    expect(label).toMatch(/did not say/i)
    expect(label, 'the sentence must deny the collapse, not just report it').toMatch(/not the same/i)
  })

  it('nothing is checked when the rule was not read', () => {
    // Inventing a rule would refuse valid work, which is the mirror of the bug.
    expect(missingRequired([f({ name: 'prompt', text: '' })], null)).toEqual([])
  })

  it('a required key that is blank is named, with the runner\'s own test', () => {
    expect(missingRequired([f({ name: 'prompt', text: '', required: true })], ['prompt'])).toEqual(['prompt'])
    // Whitespace only. `run_cli_agent` applies `.strip()`, so this must too --
    // a looser test here passes `{"prompt": "  "}` to the identical failure.
    expect(missingRequired([f({ name: 'prompt', text: ' \n ', required: true })], ['prompt'])).toEqual(['prompt'])
    expect(missingRequired([f({ name: 'prompt', text: 'do the thing', required: true })], ['prompt'])).toEqual([])
  })
})

describe('a required key is not editable chrome', () => {
  it('has no rename box and no remove control', () => {
    // A required key's NAME is the API's, not the caller's. Offering a text box
    // over it invites `prmopt`, which the platform accepts and the runner then
    // refuses after a slot and a credential have been spent.
    const { container } = render(
      <InputFields profile="claude-code" required={['prompt']} idPrefix="t" onChange={() => {}}
        fields={[f({ name: 'prompt', required: true })]} />,
    )
    const row = container.querySelector('.sbf-field.is-required')
    expect(row).not.toBeNull()
    expect(row?.querySelector('.sbf-rename'), 'a required key must not be renameable').toBeNull()
    expect(row?.querySelector('.sbf-mini'), 'a required key must not be removable').toBeNull()
    expect(row?.textContent).toContain('required')
  })
})

describe('seedFields carries work across a profile change', () => {
  it('adds a required key that is not there yet', () => {
    const seeded = seedFields([], ['prompt'])
    expect(seeded.map((s) => [s.name, s.required, s.kind])).toEqual([['prompt', true, 'text']])
  })

  it('keeps what was typed when the profile changes to another that wants it', () => {
    // claude-code -> codex. Both demand `prompt`, it is the same instruction,
    // and a control that forgets it costs a retype for nothing.
    const before = seedFields([], ['prompt'])
    before[0]!.text = 'do the thing'
    const after = seedFields(before, ['prompt'])
    expect(after[0]?.text).toBe('do the thing')
  })

  it('keeps a key the new profile does not require, because input is opaque', () => {
    // The platform never reads `input`, so an extra key is inert. Silently
    // deleting what someone typed is worse than carrying it.
    const after = seedFields([f({ name: 'model', text: 'sonnet' })], [])
    expect(after.find((s) => s.name === 'model')?.text).toBe('sonnet')
  })

  it('forces a key that BECOMES required back to the text editor', () => {
    // `required_input_keys` defines a required key as present AND a non-empty
    // STRING, so a number editor on one would offer a value the runner refuses.
    const after = seedFields([f({ name: 'prompt', kind: 'number', text: '12' })], ['prompt'])
    expect(after.find((s) => s.name === 'prompt')?.kind).toBe('text')
  })
})

/**
 * Source with its comments removed.
 *
 * BECAUSE BOTH FILES DESCRIBE THE DEFECT THEY REMOVED, at length, and a scan
 * that counted prose would report the fix as the bug. Line-based rather than
 * tokenised: a `//` is only a comment when the line STARTS with it, which
 * keeps `https://` and `'/v1/tasks'` out of the stripper's way, and `/* *\/`
 * runs are dropped whole. Both files are house-style -- block comments above
 * the code, `//` on their own lines -- so this is exact for them and is not
 * offered as a general JavaScript parser.
 */
function stripComments(src: string): string {
  const out: string[] = []
  let inBlock = false
  for (const raw of src.split('\n')) {
    const line = raw.trim()
    if (inBlock) {
      if (line.includes('*/')) inBlock = false
      continue
    }
    if (line.startsWith('/*')) {
      if (!line.includes('*/')) inBlock = true
      continue
    }
    if (line.startsWith('//') || line.startsWith('*')) continue
    if (line.startsWith('{/*')) {
      if (!line.includes('*/}')) inBlock = true
      continue
    }
    out.push(raw)
  }
  return out.join('\n')
}

describe('no JSON is typed anywhere on these two screens', () => {
  const both: Array<[string, string]> = [
    ['Submit.tsx', stripComments(SUBMIT_SRC)],
    ['SubmitWorkflow.tsx', stripComments(WORKFLOW_SRC)],
  ]

  it('the stripper leaves the code and removes the prose', () => {
    // A scan is only worth its assertions if it is scanning something. Both
    // files talk about `JSON.parse` in their docstrings; both must still
    // contain the `fetch` call that the scan is proving is all that is left.
    for (const [name, src] of both) {
      expect(src, `${name}: the stripper ate the code`).toContain('JSON.stringify')
      expect(src, `${name}: the stripper left the prose`).not.toContain('owner')
    }
  })

  it('neither file parses a caller-supplied string as JSON', () => {
    for (const [name, src] of both) {
      // `JSON.stringify` is the request body and stays. `JSON.parse` on a form
      // value is the thing that was rejected.
      expect(src.includes('JSON.parse'), `${name} still parses JSON from the form`).toBe(false)
    }
  })

  it('neither file asks anyone for a JSON object', () => {
    for (const [name, src] of both) {
      // Case-insensitive and outside comments would be better; this is the
      // cheap version that catches the label coming back. The old one read
      // exactly `input (JSON object)`.
      const label = /(['"`>][^'"`<]*\bJSON\s+object\b)/i.exec(src)
      expect(label?.[0] ?? null, `${name} offers a JSON object field again`).toBeNull()
    }
  })

  it('neither file seeds a field with an empty object literal', () => {
    for (const [name, src] of both) {
      expect(/=\s*['"]\{\}['"]/.test(src), `${name} seeds a control with the string {}`).toBe(false)
    }
  })
})

// ---------------------------------------------------------------------------
// The send panel says what its button will do (visual QA pass, epic #84)
// ---------------------------------------------------------------------------
//
// Rendered against the development fixture, which serves the runner input
// contract (`submit.fixture.test.ts` holds that it does): `claude-code` and
// `codex` require `input.prompt`, the other three require nothing. Each case
// was committed RED first.

function sendHeading(root: HTMLElement): string {
  return root.querySelector('.sbf-send h2')?.textContent ?? ''
}

describe('the send panel', () => {
  it('TS-5 / TS-16: a plan with a step that would fail cannot be sent, and says which step, beside the button', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    const go = await screen.findByRole('button', { name: 'Submit this workflow' }, { timeout: 4000 })
    // The fixture's first runner requires nothing, so the plan starts sendable.
    expect(go.hasAttribute('disabled')).toBe(false)
    expect(sendHeading(container)).toBe('Ready to send')

    // A runner that requires `input.prompt`, left blank. The step card already
    // said `Not sent`; the button stayed live and the only statement of the
    // refusal appeared ~2,100px above it after the click.
    fireEvent.change(container.querySelector<HTMLSelectElement>('select.wfb-profile')!, {
      target: { value: 'claude-code' },
    })
    expect(go.hasAttribute('disabled'), 'a plan that would fail can still be sent').toBe(true)
    expect(sendHeading(container), '"Ready to send" over a plan that cannot be sent').toBe('Not ready to send')
    const count = container.querySelector<HTMLElement>('.sbf-send .warn-text')
    expect(count, 'the send panel does not say what stops it').toBeTruthy()
    expect(count!.textContent).toContain('1 step not ready')
    // A way to the step, from beside the button. The step's problem is a
    // required key, so it lands on THAT FIELD rather than on the step's name
    // (TS-15): the name is not what has to change.
    fireEvent.click(within(count!).getByRole('button', { name: 'claude-code-1' }))
    expect(document.activeElement, 'the per-step button did not take the reader to the empty field').toBe(
      container.querySelector('.wfb-step .sbf-field.is-required textarea'),
    )
  })

  it('TS-16: the task form says it is not ready while its button is disabled', async () => {
    const { container } = render(<SubmitScreen />)
    const go = await screen.findByRole('button', { name: 'Submit one task' }, { timeout: 4000 })
    // Nothing chosen yet: the button is disabled, and the heading said "Ready".
    expect(go.hasAttribute('disabled')).toBe(true)
    expect(sendHeading(container), '"Ready to send" over a disabled button').toBe('Not ready to send')
    // A runner that needs nothing: now it is.
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    expect(go.hasAttribute('disabled')).toBe(false)
    expect(sendHeading(container)).toBe('Ready to send')
  })

  it('TS-19: names the key a runner needs without a broken article', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, { timeout: 4000 })
    const facts = (name: string) =>
      container
        .querySelector(`input[name="runner-profile"][value="${name}"]`)!
        .closest('label')!
        .querySelector('.sbf-runner-facts')!.textContent ?? ''
    expect(facts('claude-code')).toContain('anthropic key needed')
    expect(facts('codex')).toContain('openai key needed')
    expect(facts('mock')).toContain('no provider key needed')
    for (const n of ['claude-code', 'codex', 'mock']) expect(facts(n), n).not.toMatch(/needs a /)
  })
})

// ---------------------------------------------------------------------------
// The owner's decisions on epic #84, 2026-09-25 (TS-15, TS-20, TS-23)
// ---------------------------------------------------------------------------
//
// Each case was committed RED against the code it describes before the change
// landed. The keys named below are the fixture's `required_keys`, which
// `submit.fixture.test.ts` holds to the API's.

/** Everything a sighted reader sees, with the accessible-name-only text left out. */
const visible = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ')

describe('TS-15: a required key is named at its field, in the words the API used', () => {
  const blank = () =>
    render(
      <InputFields profile="claude-code" required={['prompt']} idPrefix="t" onChange={() => {}}
        fields={[f({ name: 'prompt', required: true })]} />,
    )

  it('does not paint a field the reader has not reached yet', () => {
    const { container } = blank()
    const editor = container.querySelector('.sbf-field.is-required textarea')!
    expect(editor.getAttribute('aria-invalid'), 'a freshly picked runner is painted red').toBeNull()
    expect(container.querySelector('.sbf-miss')).toBeNull()
    // The `required` tag carries it until then.
    expect(container.querySelector('.sbf-req')?.textContent).toBe('required')
  })

  it('once left empty, marks the field invalid and says under it what the runner will not do', () => {
    const { container } = blank()
    const editor = container.querySelector('.sbf-field.is-required textarea')!
    fireEvent.blur(editor)
    expect(editor.getAttribute('aria-invalid')).toBe('true')
    const line = container.querySelector('.sbf-field.is-required .sbf-miss')
    expect(line, 'the missing key is not named at its field').not.toBeNull()
    expect(visible(line!)).toBe('claude-code will not start without prompt')
    // The key in mono, as the same string the field is labelled with.
    expect(line!.querySelector('code')?.textContent).toBe('prompt')
    expect(editor.getAttribute('aria-describedby')).toBe(line!.id)
    // The full consequence stays in the accessible text.
    expect(line!.getAttribute('aria-label')).toMatch(/dispatched/)
    expect(line!.getAttribute('aria-label')).toMatch(/credential/)
    // No API jargon on the surface.
    expect(visible(container)).not.toMatch(/input\.prompt|non-empty string/)
  })

  it('clears once the field has something in it', () => {
    const { container } = render(
      <InputFields profile="claude-code" required={['prompt']} idPrefix="t" onChange={() => {}}
        fields={[f({ name: 'prompt', required: true, text: 'do the thing' })]} />,
    )
    const editor = container.querySelector('.sbf-field.is-required textarea')!
    fireEvent.blur(editor)
    expect(editor.getAttribute('aria-invalid')).toBeNull()
    expect(container.querySelector('.sbf-miss')).toBeNull()
  })

  it('names the gap in the task send panel as a button that takes the reader to the field', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, { timeout: 4000 })
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code"]')!)
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    const gap = within(panel).getByRole('button', { name: 'prompt missing' })
    expect(gap.classList.contains('sbf-mini')).toBe(true)
    expect(gap.classList.contains('sbf-bad')).toBe(true)
    fireEvent.click(gap)
    expect(document.activeElement).toBe(container.querySelector('.sbf-field.is-required textarea'))
    // The panel's own alert paragraph, in the API's words, is gone.
    expect(panel.querySelector('[role="alert"]')).toBeNull()
    expect(visible(panel)).not.toMatch(/input\.prompt|non-empty string/)
    // And "empty -- the runner gets no settings" is not said over a missing key.
    expect(visible(panel)).not.toContain('the runner gets no settings')
  })

  it('says the runner gets no settings only when nothing is required and nothing is set', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, { timeout: 4000 })
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    expect(visible(container.querySelector('.sbf-send')!)).toContain('empty — the runner gets no settings')
  })

  it('reserves "Not sent" for the result of a click, on the workflow form too', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, { timeout: 4000 })
    fireEvent.change(container.querySelector<HTMLSelectElement>('select.wfb-profile')!, {
      target: { value: 'claude-code' },
    })
    // Nothing has been clicked, so nothing has been "not sent".
    expect(visible(container), 'a live problem is worded as the outcome of a send').not.toContain('Not sent')
    expect(visible(container)).not.toMatch(/input\.prompt|non-empty string/)
    // The step card no longer carries its own copy of the missing key: the
    // field does, once it has been left.
    expect(container.querySelector('.wfb-step [role="alert"]')).toBeNull()
    // The live problem, in the new words, is what the per-step button carries.
    const step = within(container.querySelector<HTMLElement>('.sbf-send')!).getByRole('button', { name: 'claude-code-1' })
    expect(step.getAttribute('title')).toBe('claude-code will not start without prompt')
  })
})

describe('TS-20: a stage says "then", and "waits for" is said once, on each step', () => {
  it('drops the stage clause and the add-a-stage sub-line, and counts steps that run together', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, { timeout: 4000 })
    const addStage = container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!
    expect(visible(addStage).trim(), 'the add-a-stage button repeats "waits for everything above"').toBe('add a stage')
    fireEvent.click(addStage)
    const second = () => container.querySelectorAll<HTMLElement>('.wfb-stage')[1]!
    const head = () => second().querySelector('.wfb-stage-h')!
    expect(visible(head()).trim()).toBe('then')
    // The alongside cue stays.
    const alongside = second().querySelector<HTMLButtonElement>('.wfb-steps > button.wfb-add')!
    expect(visible(alongside)).toContain('runs alongside')
    fireEvent.click(alongside)
    expect(head().querySelector('.wfb-stage-say')?.textContent).toBe('2 steps run together')
    expect(visible(head())).not.toContain('everything above')
    // Once per step, on the step's own disclosure -- the control that changes it.
    const says = (visible(second()).match(/waits for/g) ?? []).length
    expect(says).toBe(second().querySelectorAll('.wfb-step').length)
  })
})

describe('TS-23: the forms carry names, facts and the runner notes, and no instructions', () => {
  it('says "no runner chosen" before a runner is chosen, and what a runner that needs nothing needs', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, { timeout: 4000 })
    expect(screen.getByText('no runner chosen')).toBeTruthy()
    expect(visible(container)).not.toContain('Choose a runner first')
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    expect(visible(container.querySelector('.sbf-input .sbf-none')!)).toBe('mock requires no input')
    expect(visible(container)).not.toContain('Add a setting below')
  })

  it('says "no settings" when the rule was not read', () => {
    const { container } = render(
      <InputFields profile="claude-code" required={null} idPrefix="t" onChange={() => {}} fields={[]} />,
    )
    expect(visible(container.querySelector('.sbf-none')!)).toBe('no settings')
  })
})
