# Opt-in LTX cost/latency planner (offline projection)

This experimental controller extension is disabled by default. It changes no
model weights, prompts, inference parameters, media quality or GPU containers.

To enable it **only after testing** in the singleton Capacity Controller:

```text
SALAD_AUTOSCALER_LTX_COST_AWARE=true
SALAD_AUTOSCALER_LTX_GPU_HOURLY_USD=0.50
SALAD_AUTOSCALER_LTX_PROJECTED_POOL_BUDGET_USD=   # optional; omit if unused
```

The policy enumerates legal replica counts for each LTX pool, preserving the
count of running jobs and stage ceilings. For each option it models remaining
work, warm workers that continue inferring during new worker bootstraps, newly
started workers, ETA, **conservative projected billable GPU seconds** and USD.
Of options predicted to meet the drain deadline and optional forward pool
budget, it chooses the lowest projected cost. When none can meet the deadline,
it chooses the fastest affordable option. When *all* options exceed the optional
budget, it refuses to expand beyond existing capacity.

The global controller still enforces project quota, stable-poll downscaling,
fenced/drained claims, active-worker protection and provider transition checks.
It remains the **only** authority able to resize Salad groups. The projection
is not a per-video spending limit or an invoice, and ignores other users'
historical shared-group spend. Prices, bootstraps and runtime distributions
must be calibrated against observed, normalized jobs before enabling.

The pricing model pessimistically bills all selected replicas from the start
of the forecast, including cold bootstrap. It cannot know provider-dependent
billable start, pending allocation, interruption or machine heterogeneity.
Its estimated GPU seconds are therefore neither the Salad invoice nor a
verified cost saving. Record cold/warm durations and run controlled A/B tests.

`docs/components/ltx25-a2v-fp8-experimental-benchmarks.md` documents that
A2V already has FP8 and Triton; do not alter those production settings for
an autoscaling experiment. For the pinned I2V setup, 1080p CPU offload and
memory-safety restrictions remain authoritative. A separate GPU experiment
must establish the cost and output quality of torch.compile or new kernels.

## Offline scenario comparison

Use `uv run --locked --extra dev python scripts/salad/simulate_ltx_capacity.py
scenario.json --json-output projection.json` with each pool's
`remaining_work_seconds`, `active_jobs`, `running_jobs`,
`current_replicas`, `max_replicas`, `bootstrap_seconds`,
`target_seconds`, `utilization`, and `hourly_usd`.
This reports each replica-count candidate and aggregate projected pool costs.
If multiple pools share a tight project cap, the singleton controller's
quota allocator remains authoritative; the simulator does not claim a feasible
project-wide allocation by itself.

This is a model, not a live A/B result. High/Medium/Low/Lowest Salad price
tiers must be compared with real interruption and retry data before any
production change; the tools do not change service priorities.
