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


## Stopped LTX replica rebounds from zero to one

A subsequent real Salad run showed `Stop` reading
`stopped/replicas=0/pending=False`, immediately followed by a new `Status`
reading `stopped/replicas=1/pending=False`. Waiting until the first
successful GET was therefore insufficient. The full remote autoscaler
configuration and job-queue snapshot were not present in that capture:
the specific cause of the rebound remains unconfirmed.

Salad's queue autoscaler adjusts desired replicas based on queued work
and `min_replicas`. LTX also has a temporary `WarmScaleOut` mode that
sets the *remote* minimum to one; it must be restored to the manifest
minimum of zero when the warm worker is no longer owned. The manager now
reports the remote minimum and current queue length in `-Action Status`.
Its `Stop` and `Prepare` paths restore an exposed nonzero remote
autoscaler minimum through the existing manifest-restore command **only
while the group is stopped and has no pending update**. They then check
three consecutive `stopped/replicas=0/pending=False` observations, spaced
15 seconds apart, including when the initial GET already reports zero. A running group aborts immediately. The parallel
preparer also detects a stale remote minimum even when the initial
replica count is zero; the prepared-image guard rejects that state.

This fix does not cancel unknown queue jobs, override a running worker,
change model code or build new images. If the remote minimum is zero but
replicas still return to one, inspect the queue's pending/running jobs
with the protected bootstrap's exhaustive preflight; a positive summary
alone can be stale. If the queue is truly empty and Salad still reasserts
one replica, collect the group's status, minimum and queue observations
for Salad support. Do not bypass the benchmark's zero-replica ownership
guard or repeatedly patch the desired replica count.


## LTX queue summary/list divergence: read-only evidence collection

The user's 2026-09-24 Salad group has `status=stopped`, remote
`queue_autoscaler.min_replicas=0` and `replicas=1`. After a manual
`replicas=0` PATCH it returned to one replica at the next 15-second
check. The Job Queue summary repeatedly reports
`current_queue_length=33`, but the user's page-by-page job enumeration
completed without identifying any `pending` or `running` jobs.
These observations support a queue/control-plane inconsistency; they
do not prove whether the summary is stale, the job list is incomplete
for another reason, or a separate scaler is writing the desired count.

Use `scripts/diagnostics/inspect_salad_queue_state.ps1 -Service ltx25`
for read-only group/queue snapshots and a complete, bounded listing
with the official list-jobs API (25 jobs per page). It prints only job
transport IDs, statuses, and creation times for active jobs; never
prints sensitive inputs or outputs and never mutates the queue or group.
If it confirms a persistent mismatch, preserve its sanitized output,
the group version, the queue name, and the time of the zero-to-one
replica rebound for Salad support. Do not run
`cleanup_salad_queue.ps1` on an unverified list or bypass the
benchmark's stopped/zero-replica guard. Replacing the queue/group or
cancelling active work requires a separate ownership check and
user-authorized migration; no automatic recreation was added.


## Safe benchmark gate after the queue discrepancy

The protected bootstrap now refuses GPU allocation if Salad reports a nonzero
`current_queue_length` while a complete job listing finds no pending/running
work. It also rechecks the queue before raising the manual replica. This
prevents a contradictory control-plane snapshot from being treated as proof
of exclusive ownership. The diagnostic is read-only; no automatic
cancellation, group recreation or repeated replica PATCH is attempted.

Both `Stop` and `Prepare` require three stable zero-replica reads even if
the first observed count was already zero. They abort on an unexpected running
group or nonzero remote autoscaler minimum. These are local orchestration
changes; the already-published Qwen v5 and LTX v9 images do not need rebuilding.

The parallel preparer also reads each existing group\'s queue summary before
any Stop or Prepare call. A missing queue-length field or nonzero reported
length aborts before mutating either group; run the read-only inspector and
resolve the discrepancy first. This does not alter a missing group\'s normal
first-time queue creation through the manager.
