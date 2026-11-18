output "profiles_file" {
  description = "The file actually parsed. Empty means none of the candidate paths existed, which the caller asserts against."
  value       = local.profiles_file
}

output "resource_classes" {
  description = "swarm_common.profiles.RESOURCE_CLASSES, parsed out of the Python source."
  value       = local.resource_classes
}

output "runner_profiles" {
  description = "swarm_common.profiles.RUNNER_PROFILES, parsed out of the Python source. `provider` is \"\" where Python has None."
  value       = local.runner_profiles
}

output "default_timeout_seconds" {
  description = "RunnerProfile.timeout_seconds default, read from the dataclass rather than assumed."
  value       = local.default_timeout
}
