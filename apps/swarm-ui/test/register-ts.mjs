// Installs ./ts-hooks.mjs. Passed as `node --import ./test/register-ts.mjs`,
// which is what `npm test` in this package does.
import { register } from 'node:module'

register('./ts-hooks.mjs', import.meta.url)
