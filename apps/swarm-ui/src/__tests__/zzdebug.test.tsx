import { fireEvent, render } from '@testing-library/react'
import { it, vi } from 'vitest'
import { load, openAt, settle } from './proseapp'
import { screenLoaded } from './screenLoaded'

const tick = async () => { for (let i = 0; i < 4; i++) { await screenLoaded(); await settle() } }
const press = (t: string) => fireEvent.click([...document.querySelectorAll('button')].find((b) => (b.textContent ?? '').trim() === t)!)
it('debug', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
  const tree = await load(null)
  vi.setSystemTime(new Date(tree.clock))
  const Shown = tree.App
  openAt('work/new')
  let r = render(<Shown />)
  await tick()
  const inp = [...document.querySelectorAll<HTMLInputElement>('label.sbf-runner input')].find((i) => i.closest('label')?.querySelector('.sbf-runner-name')?.textContent === 'claude-code')!
  fireEvent.click(inp); await tick()
  press('Start one agent'); await tick()
  console.log('DEBUG1', document.querySelector('.sbf-side')?.outerHTML.slice(0, 3000))
  r.unmount()
  openAt('work/new-workflow')
  r = render(<Shown />)
  await tick()
  press('Submit this workflow'); await tick()
  console.log('DEBUG2', [...document.querySelectorAll('[role="alert"],[role="status"],.state')].map((e) => e.outerHTML.slice(0, 400)).join('\n'))
  r.unmount()
}, 120000)
