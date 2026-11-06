// THE COMMIT THE PROSE RATCHET COMPARES WITH, and a copy of the console as it
// was there (proseapp.tsx renders both and fails a page whose words went up).
//
// WHY A BASE AND NOT A TABLE (BGD's pick, 2026-11-06, part of #2688). The
// ratchet used to hold each page to a ceiling in a JSON table that lanes
// edited. Two green changes could then fail together: one lowered a ceiling,
// the other added words that the old ceiling allowed. A check of a change
// against its own base has no such pair: two changes that each add no words
// add none together. So there is no table: the base's own pages are the
// ceiling.
//
// WHICH BASE, in order:
//   PROSE_BASE, when set: any revision git can name.
//   The merge base with origin/main, when that is not HEAD: a branch, local
//   or pushed with its history.
//   HEAD itself, when HEAD is on origin/main and the app's files are edited:
//   the uncommitted edit is the change.
//   HEAD's first parent otherwise: a pull request's merge commit (its first
//   parent is the base branch's tip), a merge-queue train (the train before
//   it) and a push to main (the commit before) all run the same delta. It is
//   read from the commit object, which a one-commit checkout still has.
// A commit git does not have (a shallow checkout) is fetched from origin at
// depth 1. A base that cannot be had is an error, never a pass.

import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'

export interface ProseBase {
  sha: string
  why: string
}

export function git(cwd: string, args: readonly string[], input?: Buffer): Buffer {
  return execFileSync('git', args, {
    cwd,
    input,
    maxBuffer: 1 << 30,
    stdio: [input === undefined ? 'ignore' : 'pipe', 'pipe', 'pipe'],
  })
}

function gitText(cwd: string, args: readonly string[]): string | null {
  try {
    return git(cwd, args).toString('utf8').trim()
  } catch {
    return null
  }
}

/** The first `parent` line of `sha`'s commit object, or null for a root commit. */
export function firstParent(cwd: string, sha: string): string | null {
  const raw = gitText(cwd, ['cat-file', '-p', sha])
  if (raw === null) throw new Error(`git cannot read commit ${sha}`)
  return /^parent ([0-9a-f]{40,64})$/m.exec(raw)?.[1] ?? null
}

/** Which commit `cwd`'s app is compared with, and why that one (see the head of this file). */
export function chooseBase(cwd: string, env: NodeJS.ProcessEnv = process.env): ProseBase {
  const named = env.PROSE_BASE
  if (named !== undefined && named !== '') {
    const sha = gitText(cwd, ['rev-parse', '--verify', `${named}^{commit}`])
    if (sha === null) throw new Error(`PROSE_BASE=${named} names no commit git has`)
    return { sha, why: `PROSE_BASE=${named}` }
  }
  const head = gitText(cwd, ['rev-parse', '--verify', 'HEAD'])
  if (head === null) throw new Error(`${cwd} is not in a git checkout: the prose ratchet needs one`)
  const mb = gitText(cwd, ['merge-base', 'HEAD', 'origin/main'])
  if (mb !== null && mb !== '' && mb !== head) return { sha: mb, why: 'the merge base with origin/main' }
  if (mb === head && (gitText(cwd, ['status', '--porcelain', '--', '.']) ?? '') !== '') {
    return { sha: head, why: 'HEAD, under an uncommitted edit' }
  }
  const parent = firstParent(cwd, head)
  if (parent === null) throw new Error(`HEAD ${head} has no parent to compare with: set PROSE_BASE`)
  return { sha: parent, why: "HEAD's first parent" }
}

/** Make sure git has `sha`'s tree, fetching it from origin at depth 1 when the checkout is shallow. */
export function ensureCommit(cwd: string, sha: string): void {
  const has = (): boolean => gitText(cwd, ['cat-file', '-e', `${sha}^{tree}`]) !== null
  if (has()) return
  let why = ''
  try {
    git(cwd, ['fetch', '--no-tags', '--depth=1', 'origin', sha])
  } catch (e) {
    why = String((e as { stderr?: Buffer }).stderr ?? e).trim()
  }
  if (!has()) throw new Error(`git has no commit ${sha} and could not fetch it from origin: ${why}`)
}

export interface BaseTree {
  /** The base's copy of the app directory: its `src`, `package.json` and `tsconfig.json`. */
  app: string
  remove: () => void
}

/**
 * The app directory `appDir` as commit `sha` had it, in a fresh directory in
 * the head's node_modules. There because vite transforms a file for jsdom only
 * inside the project's root (server.fs.allow), and node_modules is the one
 * place under it that git ignores; a bare import (`react`) from the copy walks
 * up to the head's own packages, so React is one module for both renders. The
 * tests under src/__tests__ are left out: half the bytes, and nothing the App
 * imports.
 */
export function extractApp(appDir: string, sha: string): BaseTree {
  const prefix = gitText(appDir, ['rev-parse', '--show-prefix'])
  const root = gitText(appDir, ['rev-parse', '--show-toplevel'])
  if (prefix === null || root === null) throw new Error(`${appDir} is not in a git checkout`)
  const rel = prefix.replace(/\/$/, '')
  const modules = join(appDir, 'node_modules')
  if (!existsSync(modules)) throw new Error(`${modules} is missing: install the app's packages first`)
  const app = mkdtempSync(join(modules, '.prose-base-'))
  const remove = (): void => rmSync(app, { recursive: true, force: true })
  try {
    const tar = git(root, [
      'archive',
      '--format=tar',
      rel === '' ? sha : `${sha}:${rel}`,
      '--',
      'src',
      'package.json',
      'tsconfig.json',
      ':(exclude)src/__tests__',
    ])
    execFileSync('tar', ['-x', '-C', app], { input: tar, maxBuffer: 1 << 30 })
    if (!existsSync(join(app, 'src'))) throw new Error(`commit ${sha} has no ${rel}/src`)
    return { app, remove }
  } catch (e) {
    remove()
    throw e
  }
}
