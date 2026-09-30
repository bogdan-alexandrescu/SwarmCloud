# What scripts/concurrency-test.sh concludes from its samples (#175).
#
# A pure filter, so the part of the suite that decides pass or fail is tested
# offline (tests/unit/scripts/test_concurrency_judge.py) on the exact shapes the
# suite feeds it. The suite used to print "Leases, not pods, are what counted"
# and "Backlog cost nothing" and then re-check the global limit for both.
#
# Arguments, all required in both modes:
#   $mode         "sample" (one sample in) or "persist" (-s, every judged sample)
#   $concurrency  CONCURRENCY_STATES from common.sh
#   $pending      PENDING_STATES from common.sh
#   $pre_running  CONCURRENCY_STATES without RUNNING
#   $grace        seconds a two-transaction gap may stay open (persist mode)
#
# ONE SAMPLE IS ONE READ. The suite reads the pools, the unreleased leases and
# the tasks at a single Firestore `readTime`, so everything below compares
# values from the same committed snapshot. Firestore commits a lease, its pool
# increments and the task's move to LEASED in ONE transaction
# (swarm_common.admission.acquire_lease_in_transaction), and a lease's
# released_at and its pool decrements in ONE transaction
# (release_lease_in_transaction). So `pool_mismatches` is exact: any
# difference at a consistent snapshot is a defect, with no tolerance.
#
# Two other pairs are written in SEPARATE transactions, on purpose:
#   * the worker moves a task out of the concurrency states (PARKED, or a
#     terminal state) and then releases its lease (agent_worker.control.park:
#     "Order matters. The task leaves the concurrency states first");
#   * the reconciler releases a lost worker's lease and then repairs the task's
#     state (reconciler.store.release_lease, then repair_task_state).
# Each opens a short window in which a waiting task still has a live lease, or a
# holding task has none. So those two findings are tracked per lease or task
# across samples, and only one still open after $grace seconds is a violation.

def in_set($set; $v): any($set[]; . == $v);

def judge:
  . as $s
  | [ $s.leases[].id ] as $lease_ids
  | ($s.tasks | map({ key: .id, value: . }) | from_entries) as $task_by_id
  | {
      at: $s.at,
      leases: ($s.leases | length),
      holding_tasks: ([ $s.tasks[] | select(in_set($concurrency; .state)) ] | length),

      # Invariant 3, exactly: every unit a pool says is active is held by an
      # unreleased lease that names that pool, weighted by the lease's units
      # (`units` defaults to 1, as release_lease_in_transaction reads it).
      pool_mismatches: [
        $s.pools[]
        | . as $p
        | { pool: $p.id,
            active: ($p.active // 0),
            lease_units: ([ $s.leases[] | select(in_set(.pools // []; $p.id)) | (.units // 1) ] | add // 0) }
        | select(.active != .lease_units)
      ],

      # A task in LEASED..RUNNING whose current lease is not an unreleased one:
      # capacity that no pool counts.
      unleased_holders: [
        $s.tasks[]
        | select(in_set($concurrency; .state))
        | select(((.current_lease_id // "") as $l | in_set($lease_ids; $l)) | not)
        | { key: ("task:" + .id + ":" + (.current_lease_id // "")),
            task: .id, state: .state, lease: (.current_lease_id // null) }
      ],

      # Invariant 1: an unreleased lease -- capacity -- held for a task that is
      # waiting (QUEUED, READY, PARKED, SUBMITTED).
      backlog_leases: [
        $s.leases[]
        | . as $l
        | ($task_by_id[$l.task_id] // null) as $t
        | select($t != null and in_set($pending; $t.state))
        | { key: ("lease:" + $l.id), lease: $l.id, task: $l.task_id, state: $t.state }
      ],

      # Leases whose task has not reached RUNNING. A sample with none of these
      # cannot tell a pool that counts from LEASED from one that counts from
      # RUNNING: both would agree with the leases.
      pre_running_leases: ([ $s.leases[] | ($task_by_id[.task_id].state // "") as $st
                             | select(in_set($pre_running; $st)) ] | length)
    };

# Every finding that stayed open longer than $grace, with how long it was seen.
def persistent($field):
  [ .[] | .at as $at | .[$field][] | . + { at: $at } ]
  | group_by(.key)
  | map({ first: (map(.at | fromdate) | min), last: (map(.at | fromdate) | max), row: .[0] })
  | map(select(.last - .first > $grace))
  | map(.row + { seconds: (.last - .first), first_seen: (.first | todate), last_seen: (.last | todate) } | del(.at));

if $mode == "sample" then judge
elif $mode == "persist" then { backlog_leases: persistent("backlog_leases"), unleased_holders: persistent("unleased_holders") }
else error("concurrency-judge.jq: unknown mode \($mode)")
end
