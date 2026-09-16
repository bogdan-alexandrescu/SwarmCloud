# The Python catalogue, read as text.
#
# terraform/infra/locals.tf restates swarm_common/profiles.py because terraform
# cannot import Python, and a restatement that nothing compares is a copy that
# drifts. Everything else in tests/terraform asserts terraform against terraform,
# which cannot catch that: if profiles.py changes, locals.tf and a hand-copied
# assertion drift away from Python together and the suite still passes. The
# symptom in production is a Job resource sized for a profile the worker is not
# running -- exactly the class of bug nobody finds by reading.
#
# So this module reads the real file off disk and parses the two structures that
# terraform mirrors. It is a text parser, not an interpreter, and that is the
# trade: it cannot evaluate Python, so it pins the LITERAL FORM of the catalogue
# as well as its values. A reformat of profiles.py that the parser cannot read
# fails loudly here -- `profiles_file` and the two counts are asserted by the
# caller -- rather than silently matching nothing. Loud is the correct failure:
# swarm_common is frozen, so its literal form does not change casually, and a
# parser that returns an empty map on a miss would be worse than no detector at
# all.

locals {
  # Candidates in order. `path.module` works for the normal case (the module is
  # three levels below the repository root); the plain relative path covers a
  # run whose working directory is tests/terraform. fileexists() picks, and an
  # empty result is surfaced rather than swallowed -- see `profiles_file`.
  candidates = compact([
    var.profiles_path,
    "${path.module}/../../../apps/common/swarm_common/profiles.py",
    "../../apps/common/swarm_common/profiles.py",
  ])

  found = [for p in local.candidates : p if fileexists(p)]

  profiles_file = length(local.found) > 0 ? local.found[0] : ""

  source = local.profiles_file == "" ? "" : file(local.profiles_file)

  # --- RESOURCE_CLASSES ----------------------------------------------------
  #
  #   "standard": ResourceClass("standard", cpu=4, memory_gib=8, disk_gib=20, units=1,
  resource_class_matches = regexall(
    "ResourceClass\\(\"([a-z]+)\",\\s*cpu=([0-9]+),\\s*memory_gib=([0-9]+),\\s*disk_gib=([0-9]+),\\s*units=([0-9]+)",
    local.source,
  )

  resource_classes = {
    for m in local.resource_class_matches :
    m[0] => {
      cpu        = tonumber(m[1])
      memory_gib = tonumber(m[2])
      disk_gib   = tonumber(m[3])
      units      = tonumber(m[4])
    }
  }

  # --- RUNNER_PROFILES -----------------------------------------------------
  #
  # Cut the dict literal out first: from `RUNNER_PROFILES: dict` up to the next
  # top-level `def`, which is resolve_backend(). Without that cut the last chunk
  # would run to the end of the file and pick up identifiers out of the function
  # body.
  runner_section = local.source == "" ? "" : split("\ndef ", split("RUNNER_PROFILES: dict", local.source)[1])[0]

  # Each entry is a multi-line RunnerProfile(...) call, so the section is split
  # on the constructor and element 0 (everything before the first one) dropped.
  runner_chunks = local.runner_section == "" ? [] : slice(
    split("RunnerProfile(", local.runner_section),
    1,
    length(split("RunnerProfile(", local.runner_section)),
  )

  # `timeout_seconds: int = 3600` on the dataclass. Read rather than assumed: a
  # profile that states no timeout takes this, and hard-coding it here would put
  # back exactly the hand-copied number this module exists to remove.
  default_timeout_matches = regexall("timeout_seconds: int = ([0-9]+)", local.source)

  default_timeout = length(local.default_timeout_matches) > 0 ? tonumber(local.default_timeout_matches[0][0]) : 0

  runner_profiles = {
    for c in local.runner_chunks :
    regexall("name=\"([a-z0-9-]+)\"", c)[0][0] => {
      image          = regexall("image=\"([a-z0-9-]+)\"", c)[0][0]
      resource_class = regexall("resource_class=\"([a-z]+)\"", c)[0][0]
      backend        = regexall("backend=Backend\\.([A-Z_]+)", c)[0][0]

      # "" rather than null: the two are the same statement here ("this runner
      # needs no provider key"), and a string keeps the map's element type
      # uniform for the caller's comparison.
      provider = length(regexall("provider=\"([a-z]+)\"", c)) > 0 ? regexall("provider=\"([a-z]+)\"", c)[0][0] : ""

      timeout_seconds = length(regexall("timeout_seconds=([0-9]+)", c)) > 0 ? tonumber(regexall("timeout_seconds=([0-9]+)", c)[0][0]) : local.default_timeout

      # secrets=("ANTHROPIC_API_KEY",) -> the env var names the Job must wire to
      # this tenant's secret. Terraform's secret_env map is keyed by these, so a
      # rename in Python that terraform did not follow shows up here. Split on
      # the comma rather than stripping it, so a two-secret tuple stays two
      # names instead of becoming one run-on string.
      secret_env_names = sort([
        for raw in(
          length(regexall("secrets=\\(([^)]*)\\)", c)) > 0
          ? split(",", regexall("secrets=\\(([^)]*)\\)", c)[0][0])
          : []
        ) :
        trimspace(replace(raw, "\"", ""))
        if trimspace(replace(raw, "\"", "")) != ""
      ])
    }
  }
}
