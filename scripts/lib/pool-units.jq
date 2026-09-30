# The most units ONE task can need from a pool, for scripts/pool-limit.sh (#66).
#
# A pool whose limit is above 0 and below that number can never admit such a
# task: `SlotPool.has_capacity` is `active + units <= effective_limit`, false
# even at active 0. On 2026-09-24 resource:browser sat at 1 against a browser
# task's 2 units and refused it on every drain while the screen said "busy".
#
# Arguments:
#   $classes   terraform output `resource_classes`  ({class: {units, ...}})
#   $profiles  terraform output `runner_profiles`   ({profile: {resource_class, backend, provider}})
#   $pool      the pool name
# Both outputs are the mirror of swarm_common.profiles that
# tests/terraform/catalogue.tftest.hcl holds to the Python, so this reads the
# contract's numbers without restating them.
#
# Output: {class, units} of the largest class that reaches the pool, or null
# when no task in the catalogue can reach it. The reach follows
# swarm_common.models.pool_names_for:
#   resource:<c>                  class c
#   runner:<p>                    profile p's class
#   backend:<b>                   every profile whose backend is b
#   provider:<p>[:tenant:<t>]     every profile whose provider is p
#   global, tenant:<t>            every profile
#   anything else                 every class (the conservative answer)

def classes_of_profiles(f): [ $profiles[] | select(f) | .resource_class ];

( if   ($pool | startswith("resource:")) then [ $pool | ltrimstr("resource:") ]
  elif ($pool | startswith("runner:"))   then [ ($profiles[$pool | ltrimstr("runner:")] // {}).resource_class // empty ]
  elif ($pool | startswith("backend:"))  then ($pool | ltrimstr("backend:")) as $b | classes_of_profiles(.backend == $b)
  elif ($pool | startswith("provider:")) then ($pool | ltrimstr("provider:") | split(":tenant:") | .[0]) as $p
                                              | classes_of_profiles(.provider != null and .provider == $p)
  elif $pool == "global" or ($pool | startswith("tenant:")) then classes_of_profiles(true)
  else [ $classes | keys[] ]
  end ) as $reach
| [ $reach[] | { class: ., units: ($classes[.].units // null) } | select(.units != null) ]
| if length == 0 then null else max_by(.units) end
