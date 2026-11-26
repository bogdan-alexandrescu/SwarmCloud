import { it } from 'vitest'
import { writeFileSync } from 'node:fs'
import { allOpeners } from './prosestates.openers'
import { OVERLAYS } from './prosestates'
it('list', () => {
  const all = allOpeners(OVERLAYS)
  writeFileSync(process.env.TMPDIR + '/openers.txt', all.map((o) => `${o.id}\t${o.line}`).join('\n') + '\n')
})
