// Node module hooks that let `node --test` import this app's TypeScript
// directly, with no build step, no bundler config and no new dependency.
//
// WHY THIS EXISTS. The check logic in `src/checks.ts` decides what the landing
// screen says is wrong. It is pure -- Results in, `Check[]` out, no React, no
// DOM, no clock it does not receive -- and it is exactly the kind of thing that
// has to be exercised rather than read, because its failure mode is a panel
// that stays silent while a workflow is stalled. There was no JavaScript test
// runner in this repository at all, so `tests/unit/control_plane/` reads the
// TypeScript as TEXT. Text can pin a threshold and a comparison operator; it
// cannot show that a stalled workflow produces a problem row and a healthy one
// does not.
//
// WHY NOT A BUNDLER. `transformWithEsbuild` is re-exported by `vite`, which is
// already a declared devDependency of this package, so this file adds nothing
// to package.json and nothing to the lockfile. Importing `esbuild` directly
// would work today only because vite happens to hoist it -- an undeclared
// dependency that breaks the first time vite changes how it vendors its own
// tools.
//
// SCOPE. Type ANNOTATIONS are erased and that is all. No emit-time semantics
// are relied on -- no `enum`, no `namespace`, no decorators, no parameter
// properties anywhere in the modules this loads -- so the JavaScript node runs
// is the JavaScript the browser bundle runs. `tsc -b --noEmit` (npm run
// typecheck) remains the thing that checks the types themselves; this only
// removes them.

import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { transformWithEsbuild } from 'vite'

/**
 * Resolve an extensionless relative import the way the bundler does.
 *
 * `import { x } from './types'` is what every module in src/ writes, and ESM
 * requires the extension. Only `.ts` is tried: a `.tsx` module would drag React
 * in, and nothing the check logic depends on may do that -- if this ever has to
 * grow a `.tsx` case, something rendering has leaked into the pure layer and
 * the right fix is on that side, not here.
 */
export async function resolve(specifier, context, next) {
  if (specifier.startsWith('.') && !/\.[a-z]+$/i.test(specifier)) {
    return next(`${specifier}.ts`, context)
  }
  return next(specifier, context)
}

export async function load(url, context, next) {
  if (url.endsWith('.ts')) {
    const path = fileURLToPath(url)
    const { code } = await transformWithEsbuild(await readFile(path, 'utf8'), path, {
      loader: 'ts',
    })
    return { format: 'module', shortCircuit: true, source: code }
  }
  return next(url, context)
}
