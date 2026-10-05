import { afterAll, expect } from 'vitest'
import { configure, getConfig } from '@testing-library/react'
import { appendFileSync } from 'node:fs'

const OUT = process.env.WAIT_LOG ?? '/tmp/waits.tsv'
const prev = getConfig().asyncWrapper
configure({
  asyncWrapper: async (cb) => {
    const t0 = performance.now()
    const where = (new Error().stack ?? '').split('\n').filter((l) => /\.test\.tsx?/.test(l)).slice(0, 2).map((l) => l.trim().replace(/.*\/src\//, '')).join(' < ')
    let ok = 'ok'
    try {
      return await prev(cb)
    } catch (e) {
      ok = 'FAIL'
      throw e
    } finally {
      const ms = performance.now() - t0
      if (ms > Number(process.env.WAIT_MIN ?? 300)) {
        const s = expect.getState()
        appendFileSync(OUT, `${Math.round(ms)}\t${ok}\t${s.testPath?.split('/').pop()}\t${s.currentTestName}\t${where}\n`)
      }
    }
  },
})
afterAll(() => {})
