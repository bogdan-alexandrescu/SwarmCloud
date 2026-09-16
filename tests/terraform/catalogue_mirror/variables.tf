variable "profiles_path" {
  description = <<-EOT
    Path to the frozen swarm_common/profiles.py. Empty resolves it from the
    module's own location, which is what every normal run does; setting it is
    for a test that wants to point the parser at a fixture.
  EOT
  type        = string
  default     = ""
}
