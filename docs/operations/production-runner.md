# End-to-end production runner: retired

The previous `run_video_factory.ps1` / `run_production.py` orchestration
depended on the removed scene/shot planning bots, and has been removed rather
than left pointing at missing imports or converting B2 into the old schema.
**The B1.1 → B1.2 → B2 runner does not yet create a finished video.**

To run the active bots, use `scripts/pipeline/run_b_pipeline.py` as documented
in [the B pipeline guide](../components/b-pipeline.md).

The independent Salad services are **not** retired: the service manifest,
worker entrypoints, Postgres/R2 job transport, Capacity Controller and
service-level smoke tests remain available. The controlled Qwen, Breeze/Fish,
Whisper, LTX and Real-ESRGAN runners are retained for existing explicit stage
artifacts and diagnostics; they must not be presented as a supported
B2-to-video pipeline.

Restoring a video-wide runner requires adapting the downstream contracts to
string beat IDs, the avatar / avatar+media / image / video strategies,
source-preserving timing, asset selection and the compositor without
discarding information. Add end-to-end and queue/capacity regression tests
before restoring automated Salad orchestration.
