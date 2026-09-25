// ONE WAY FOR A TEST TO REGISTER A READ IN THE PROBE REGISTRY.
//
// The registry is keyed by ROUTE (CH-18), and the value `noteFixtureProbe`
// takes is the one `route()` builds, not a string -- so a test that wants a
// cell in the dock names the route the way a loader does. Kept in one place so
// the tests that only need "some read happened" do not each restate how.

import { noteFixtureProbe, type ApiErrorKind } from '../fetch'

/** Register one read of `template`, as the fixture path does. */
export function noteProbe(template: string, latencyMs: number, ok: boolean, kind?: ApiErrorKind): void {
  noteFixtureProbe(template, latencyMs, ok, kind)
}
