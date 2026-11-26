/**
 * HOST-SCOPED SLOTS AND THEIR RULE, as swarm_api/workspace_secrets.py states
 * them (`HOST_SCOPED_SLOTS`, `MAX_SLOT_HOSTS`, `SLOT_HOST`, `slot_hosts`).
 * The API is still the gate; these let the form refuse before the call, in
 * the API's words, and a test holds them to that module.
 */
export const HOST_SCOPED_SLOTS: readonly string[] = ['cf-access']
export const MAX_SLOT_HOSTS = 16
/** A lower-case DNS name with a dot: no port, IP literal, wildcard, scheme or path. Matched exactly. */
export const SLOT_HOST = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/
/** `slot_hosts`' refusal of an enabled grant with no host. */
export const hostsNeeded = (slot: string) =>
  `a grant into the ${slot} slot names the hosts its secret may be sent to (\`hosts\`, 1 to ${MAX_SLOT_HOSTS} host names); it is sent to no other`
/** `slot_hosts`' refusal of a list over the cap. */
export const HOSTS_TOO_MANY = `\`hosts\` is a list of 1 to ${MAX_SLOT_HOSTS} host names`
/** `slot_hosts`' refusal of a host `SLOT_HOST` does not match. */
export const HOST_REFUSED = 'each host is a lower-case DNS name with a dot, matched exactly: no scheme, port, path, wildcard or IP address'

export function isHostScoped(slot: string | null): boolean {
  return slot !== null && HOST_SCOPED_SLOTS.includes(slot)
}

/** Why `host` cannot join `hosts`, in the API's words, or null when it can. */
export function hostProblem(hosts: readonly string[], host: string): string | null {
  if (!SLOT_HOST.test(host)) return HOST_REFUSED
  if (hosts.includes(host)) return `${host} is listed already`
  if (hosts.length >= MAX_SLOT_HOSTS) return HOSTS_TOO_MANY
  return null
}
