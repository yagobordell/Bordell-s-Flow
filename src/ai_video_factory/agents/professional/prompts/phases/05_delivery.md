# Phase 5 — deterministic delivery (no model call)

Once whole-video B1.2 cut materialization, B2/B2.1 visual
validation, CTA checks, image grid planning and editor anchors are all
green, the Python controller saves outputs, final plan, rhythm review,
usage accounting and corresponding legacy B-stage output.json files.
The controller does NOT invoke the LLM to assert its own correctness.

All script wording remains literally identical to the authoritative script.
The postproduction TTS/STT, images, motion and video pipeline runs ONLY
after this delivery gate, never during a --stop openai-agent test.
