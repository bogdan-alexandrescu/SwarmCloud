# ACCOUNTS-TIMELINE review harness (never shipped)

The real console, with `./api`'s accounts and parked-task reads answered from
proposal B's scenarios (`localStorage['harness.s']`: `b2` 09:04Z, `b3` 14:04Z,
`b6` empty), so the Timeline renders against mock-up B's frames.

    ln -s "$PWD/apps/swarm-ui/node_modules" scratchpad/accounts-timeline/harness/node_modules
    cd apps/swarm-ui && NODE_ENV=development npx vite build --mode development \
      -c ../../scratchpad/accounts-timeline/harness/vite.config.ts
    swarm-handoff serve /tmp/tl-harness

Open `/#capacity/accounts?view=timeline` after setting `harness.s`, with the
page clock fixed at the scenario's instant (Playwright `clock.setFixedTime`
then `clock.resume()`, or fonts never finish loading).
