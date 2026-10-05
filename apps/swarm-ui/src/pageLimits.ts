/*
 * THE TWO TASK-PAGE SIZES, IN A MODULE THAT IMPORTS NOTHING.
 *
 * The check layer (checks.ts) says which window its Failures figure counts
 * (owner QA R12), so it names both sizes. It is pure -- `node --test` loads it
 * through test/ts-hooks.mjs with no bundler -- and reading the full page's size
 * from `api.ts` pulled in the client and its `import.meta.env.DEV`, which only
 * Vite defines, so the check tests died on import. Both values live here;
 * `api.ts` and `agentlist.ts` re-export them for the screens that already
 * import them from there.
 */

/**
 * The largest page `GET /v1/tasks` serves. Page size caps at 200 server-side
 * (deps.py:194-199); asking for more is silently clamped, which would make
 * "200 tasks" look like the whole truth.
 */
export const TASK_PAGE_LIMIT = 200

/**
 * THE PAGE THE LIST READS AT PHONE WIDTH -- AND SO THE PAGE THE OVERVIEW
 * COUNTS OVER THERE (OV-10). Why 50 and why only on a phone is `Agents.tsx`'s
 * to say (docs/web-ui/03-agents-and-workflows.md §2.5: the payload of a
 * 200-row page on a 5-second poll).
 *
 * IT IS HERE, NOT IN `Agents.tsx`, BECAUSE THE OVERVIEW READS IT TOO. OV-10's
 * guarantee is that the failures item's figure and the list its link opens
 * describe one population, because the list filters client-side over the same
 * task read the check counts. When the list started reading 50 rows at phone
 * width and the Overview kept reading 200, "7 failed among the 200 most
 * recent" could open a list that said nothing failed. Both read this value at
 * `phoneWidth()` and the api's full page otherwise; one value, so the two
 * cannot drift apart again.
 */
export const PHONE_PAGE_LIMIT = 50
