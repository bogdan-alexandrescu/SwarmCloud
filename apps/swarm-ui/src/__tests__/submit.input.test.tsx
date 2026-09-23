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
import { render } from '@testing-library/react'

import SUBMIT_SRC from '../Submit.tsx?raw'
import WORKFLOW_SRC from '../SubmitWorkflow.tsx?raw'
import {
  InputFields,
  buildInput,
  missingRequired,
  seedFields,
  type BrowserAction,
  type InputField,
  type ValueKind,
} from '../Submit'

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
