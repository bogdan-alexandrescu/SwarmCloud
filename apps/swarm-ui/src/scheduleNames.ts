import { useEffect, useState } from 'react'
import type { Schedule } from './api'

/**
 * A SCHEDULE'S NAME BY ITS ID, as the list last read it (QA2 automate2-15):
 * the tab's title and an approval's "What asked for it" named a schedule by
 * its id ("sch_a · Schedules") though the list under the open sheet had read
 * its name. The list records every name it reads; `useScheduleName` gives the
 * name for an id, or null until one is read, and the caller keeps the id then.
 */
const SCHEDULE_NAMES = new Map<string, string>()
export const SCHEDULE_NAMES_EVENT = 'swarm:schedule-names'

export function rememberScheduleNames(rows: readonly Pick<Schedule, 'schedule_id' | 'name'>[]): void {
  let changed = false
  for (const r of rows) {
    if (r.name.trim() === '' || SCHEDULE_NAMES.get(r.schedule_id) === r.name) continue
    SCHEDULE_NAMES.set(r.schedule_id, r.name)
    changed = true
  }
  if (changed) globalThis.dispatchEvent?.(new Event(SCHEDULE_NAMES_EVENT))
}

export function scheduleNameOf(id: string): string | null {
  return SCHEDULE_NAMES.get(id) ?? null
}

export function useScheduleName(id: string | null): string | null {
  const [name, setName] = useState<string | null>(() => (id === null ? null : scheduleNameOf(id)))
  useEffect(() => {
    const again = () => setName(id === null ? null : scheduleNameOf(id))
    again()
    globalThis.addEventListener?.(SCHEDULE_NAMES_EVENT, again)
    return () => globalThis.removeEventListener?.(SCHEDULE_NAMES_EVENT, again)
  }, [id])
  return name
}
