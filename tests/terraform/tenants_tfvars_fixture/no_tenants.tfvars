# A tfvars with no top-level tenants block. terraform/bootstrap must refuse to
# plan against it rather than grant the deployer on the platform accounts alone
# and let the next tenant's release fail on a 403.

project_id = "saga-agents-staging"

service_max_instances = {
  swarm-api = 5
}
