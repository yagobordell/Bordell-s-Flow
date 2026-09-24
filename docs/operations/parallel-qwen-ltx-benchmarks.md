# Parallel Qwen INT8 / LTX A2V benchmark on Salad

Use the branch that contains PR #204 and stop any previous benchmark process.
The two services use separate RTX 5090 workers and separate queues, but share
Docker Desktop and the local checkout. Update the checkout and run uv sync
**before** starting either benchmark; do not update the checkout mid-run.

## 1. Local preflight (one PowerShell window)

Run from the repository root:

powershell -ExecutionPolicy Bypass -NoProfile

    Set-ExecutionPolicy -Scope Process RemoteSigned -Force
    git fetch origin
    git switch test/ltx25-a2v-five-run-benchmark-20260924
    git pull --ff-only origin test/ltx25-a2v-five-run-benchmark-20260924
    uv sync --locked --extra dev
    .\scripts\salad\manage_salad_worker.ps1 -Service qwen_image_21 -Action Status
    .\scripts\salad\manage_salad_worker.ps1 -Service ltx25 -Action Status
    .\scripts\salad\check_salad_gpu_availability.ps1 -Service all

Both groups must be stopped/zero-replica/no-pending-change. The availability
check queries Salad's live high-priority supply for each service's exact GPU,
CPU, memory and storage requirements. It is a snapshot, not a reservation.
Zero available GPUs is a reason to review the requested resources or wait for
capacity, not to force a different GPU class for a comparable benchmark.

    .\scripts\smoke\prepare_parallel_benchmarks.ps1

Preparation sequentially builds or reuses the two immutable image digests,
updates Salad only if required, and leaves both groups stopped. Never run
the preparer while a benchmark is active.

## 2. Run both in separate PowerShell processes

Window Qwen, from the same checkout (the prompt file must contain the exact
historical prompt):

    Set-ExecutionPolicy -Scope Process RemoteSigned -Force
    .\scripts\smoke\benchmark_qwen_image_21.ps1 -UsePreparedImage -PromptFile ".\data\input\benchmarks\qwen-prompt.txt" -Seed 4242

Window LTX, from the same checkout (use the actual existing PNG and 4-6 s
audio files):

    Set-ExecutionPolicy -Scope Process RemoteSigned -Force
    .\scripts\smoke\benchmark_ltx25_a2v.ps1 -UsePreparedImage -AvatarImage ".\data\input\avatar\monje.png" -Audio ".\data\input\avatar\monje.wav" -Prompt "An elderly monk speaking calmly to the camera." -Seed 4242

Use the historical Qwen seed if known, rather than 4242. Both runners reuse
the already published images: they do not build or patch Salad during the
parallel measurement. The Qwen benchmark uses an isolated 1536x864 profile
to compare the prior 52.29 s run; production remains 1280x736. LTX uses the
1280x720 fast distilled 8+3 A2V profile. Each runner keeps its own worker
alive for five sequential jobs and attempts to stop only its own worker.

## 3. After both complete

    .\scripts\salad\manage_salad_worker.ps1 -Service qwen_image_21 -Action Status
    .\scripts\salad\manage_salad_worker.ps1 -Service ltx25 -Action Status

Confirm both are stopped with replicas=0. Results are written to separate
batch directories under data/output/deployment-validation/ for Qwen and LTX.
Manually inspect all five Qwen PNGs and LTX MP4s; the timing summary does
not certify visual quality or lip sync.

## Allocation diagnostic

The protected bootstrap times out if neither a started instance nor an
assigned GPU appears within the configured allocation watchdog. This now
also covers Salad's group-level "allocating" state with zero instances.
Allocation delay depends on available compatible Salad nodes; the watchdog
limits futile waiting but does **not** make capacity appear. Avoid
reallocating an instance that has not been assigned a node, recreating a
stalled group, silently lowering RAM, or changing the GPU class merely
to obtain a faster cold start. If no high-priority RTX 5090 matches LTX's
CPU/RAM/storage profile, review the availability diagnostic and actual
memory/CPU/storage usage before proposing a different manifest profile.

Salad bills for running instances, not allocation, image download or
container cold start; two simultaneous *running* RTX 5090s incur two
instances' charges.


## Interrupted preparation: stopped group with one replica

Salad can finish a container image upgrade with `pending_change=false` while
the group still reports a residual desired replica (`status=stopped`,
`replicas=1`). The manager used to consider the follow-up replica PATCH
settled as soon as `pending_change` became false, even if `replicas` had
not converged to zero. This produced a premature Prepare failure after a
successful image build, push and version upgrade.

The manager's Prepare and Stop cleanup now wait for **all three** state
conditions (stopped, replicas=0 and pending_change=false) after the
zero-replica PATCH; they poll for at most 180 seconds and fail with current
state if Salad does not converge. The wait aborts immediately if another
workflow has started the group. The parallel preparer checks both groups
for stopped/no-pending first, then uses the existing Stop lifecycle for
a stopped group with a residual replica before inspecting immutable
published image digests. It never rebuilds an already published image just
because a previous Prepare aborted in the normalization phase.

To resume after the September 24 LTX v9 preparation failure, first check
both groups are stopped and have no pending change, update the checkout
to PR #204, and rerun `scripts/smoke/prepare_parallel_benchmarks.ps1`.
If either group is still running or updating, do not run the preparer.
If Salad still refuses zero replicas after the bounded wait, inspect the
remote group and queue; do not recreate the group, retry builds, or launch
GPU benchmarks while a residual replica remains.
