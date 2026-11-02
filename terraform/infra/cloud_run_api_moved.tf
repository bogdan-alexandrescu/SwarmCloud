# swarm-api's SERVICE MOVES TO ITS OWN CALL OF THE MODULE (#3041; main.tf
# `module "cloud_run_api"` says why it rolls first).
#
# One instance, one known key, so one `moved` block: the release that ships
# this plans the service as moved and updated in place, never destroyed and
# recreated -- a recreate would drop swarm-api's two warm instances and every
# running attempt's lease renewal with them. A state that never held the old
# address (a rebuilt environment) plans nothing for it.
#
# Its run.invoker grants do not move: they stay at
# module.cloud_run.google_cloud_run_v2_service_iam_member.invokers["swarm-api:*"]
# through the module's `invokers_elsewhere`, because their keys are per tenant
# and per api_invokers member and no `moved` block can enumerate them.
moved {
  from = module.cloud_run.google_cloud_run_v2_service.this["swarm-api"]
  to   = module.cloud_run_api.google_cloud_run_v2_service.this["swarm-api"]
}
