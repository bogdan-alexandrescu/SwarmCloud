// THE HIGHLIGHTER'S LEXER AND ITS LANGUAGE TABLE (lane DIFF1).
//
// The one property everything else rests on: a line's tokens concatenate to
// the line, character for character, whatever the line holds. The viewer
// draws the tokens; a lexer that dropped or doubled a character would change
// the diff on screen.
//
// MUTATION: skip a character between tokens, treat `a#b` as a comment in
// shell, or colour a `.txt` -- each goes red below.

import { describe, expect, it } from 'vitest'

import { tokenize, type Token } from '../../diff/highlight'
import DIFFVIEW_SRC from '../../diff/DiffView.tsx?raw'
import LOADER_SRC from '../../diff/useHighlighter.ts?raw'
import { HIGHLIGHT_MAX_LINE, highlightLanguage, languageOf, type Lang } from '../../diff/lang'
import { parseUnifiedDiff } from '../../diff/parse'

const kinds = (ts: Token[]): string[] => ts.filter((t) => t.k !== null).map((t) => `${t.k}:${t.s}`)
const join = (ts: Token[]): string => ts.map((t) => t.s).join('')

describe('tokenize', () => {
  const LANGS: Lang[] = ['c', 'ts', 'go', 'rust', 'py', 'sh', 'yaml', 'json', 'css', 'hcl']
  const LINES = [
    '',
    'const x = "a \\" b" // tail',
    "x = '''unterminated",
    '/* open comment',
    'a#b # real comment',
    '  - name: "v" # c',
    '{"key": 1.5e3, "v": true}',
    '<script>alert(1)</script>',
    '\t\tmixed\ttabs and ‘unicode’ 🚀 0x1F',
    '"unterminated \\',
  ]

  it('joins back to the line exactly, in every language, for every line', () => {
    let n = 0
    for (const lang of LANGS) {
      for (const line of LINES) {
        expect(join(tokenize(line, lang))).toBe(line)
        n += 1
      }
    }
    expect(n).toBe(LANGS.length * LINES.length)
  })

  it('finds keywords, strings, numbers and comments in TypeScript', () => {
    expect(kinds(tokenize('const n = 42 // the answer', 'ts'))).toEqual(['kw:const', 'num:42', 'com:// the answer'])
    expect(kinds(tokenize('return `t${x}` + "s"', 'ts'))).toEqual(['kw:return', 'str:`t${x}`', 'str:"s"'])
    expect(kinds(tokenize('a /* b */ c', 'ts'))).toEqual(['com:/* b */'])
  })

  it('does not take a keyword out of the middle of a word, or a number out of a name', () => {
    expect(kinds(tokenize('constant iffy v2', 'ts'))).toEqual([])
  })

  it('reads Python comments and triple-quoted strings', () => {
    expect(kinds(tokenize('def f(): return """doc""" # c', 'py'))).toEqual(['kw:def', 'kw:return', 'str:"""doc"""', 'com:# c'])
  })

  it('takes # as a comment in shell and YAML only at the start or after a space', () => {
    expect(kinds(tokenize('echo a#b # c', 'sh'))).toEqual(['com:# c'])
    expect(kinds(tokenize('url: http://x/#frag', 'yaml'))).toEqual(['key:url'])
  })

  it('marks JSON and YAML keys apart from values', () => {
    expect(kinds(tokenize('{"name": "v", "n": null}', 'json'))).toEqual(['key:"name"', 'str:"v"', 'key:"n"', 'kw:null'])
    expect(kinds(tokenize('  - image: "x" # pinned', 'yaml'))).toEqual(['key:image', 'str:"x"', 'com:# pinned'])
  })
})

describe('languageOf and highlightLanguage', () => {
  it('names the lexer by extension or file name, and nothing for an unknown one', () => {
    expect(languageOf('apps/swarm-ui/src/diff/DiffView.tsx')).toBe('ts')
    expect(languageOf('a/b.PY')).toBe('py')
    expect(languageOf('terraform/infra/main.tf')).toBe('hcl')
    expect(languageOf('apps/x/Dockerfile')).toBe('sh')
    expect(languageOf('notes.txt')).toBeNull()
    expect(languageOf('.gitignore')).toBeNull()
    expect(languageOf('README')).toBeNull()
  })

  it('gives no lexer to a binary file or to one over the size cap', () => {
    const patch = (n: number): string =>
      ['diff --git a/a.ts b/a.ts', '--- a/a.ts', '+++ b/a.ts', `@@ -0,0 +1,${n} @@`, ...Array.from({ length: n }, () => '+x'), ''].join('\n')
    const small = parseUnifiedDiff(patch(3))
    const big = parseUnifiedDiff(patch(401))
    if (!small.ok || !big.ok) throw new Error('fixture did not parse')
    expect(highlightLanguage(small.files[0]!)).toBe('ts')
    expect(highlightLanguage(big.files[0]!)).toBeNull()
    expect(highlightLanguage({ ...small.files[0]!, binary: true })).toBeNull()
    expect(HIGHLIGHT_MAX_LINE).toBeGreaterThan(200)
  })
})

describe('the lexer is loaded lazily', () => {
  /** Every `import ... from './highlight'` in a source that is not `import type`: a static import puts the lexer in the page's bundle. */
  const staticImports = (src: string): string[] =>
    (src.match(/^import\s+(?!type\b)(?:[^'\n]|\n(?!import))*?from\s+'\.\/highlight'/gm) ?? [])

  it('is reached from the viewer only through a dynamic import', () => {
    expect(staticImports(DIFFVIEW_SRC)).toEqual([])
    expect(staticImports(LOADER_SRC)).toEqual([])
    expect(LOADER_SRC).toMatch(/import\('\.\/highlight'\)/)
    // The viewer's own reference to it is a type, erased at build.
    expect(DIFFVIEW_SRC).toMatch(/^import type \{ Token \} from '\.\/highlight'$/m)
  })
})
