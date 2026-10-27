import { useEffect, useSyncExternalStore } from 'react'
import { loadMe } from './api'
import { currentZone, learnPerson, subscribeZone } from './timezone'

/**
 * The page's zone, re-rendering when the person picks another. Kept apart from
 * `timezone.ts` so that module stays free of React and the API: the pure check
 * layer `node --test` loads reads the zone through `overviewReads.ts`.
 */
export function useTimeZone(): string {
  // `loadMe` is read only when the read is made, as before the split: a test that mocks './api' without it still mounts.
  useEffect(() => learnPerson((options) => loadMe(options)), [])
  return useSyncExternalStore(subscribeZone, currentZone, currentZone)
}
