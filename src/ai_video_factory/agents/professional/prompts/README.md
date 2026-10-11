# OpenAI Professional Agent — instruction package

These files are packaged under ai_video_factory.agents.professional, deliberately isolated from Casero. Use load_phase_instructions(phase) to provide the explicit instruction set on each call to the SAME logical agent. Python, not the model, controls phase changes and delivery.

## Authority

1. Python source script and deterministic validation are authoritative.
2. Immutable requirements in rules/ are higher priority than phase tasks.
3. Phase files describe responsibilities and must not override rules.
4. Memory files are *preference hints*, never permissions to ignore rules. Only explicit approved user feedback may update memory.
5. Source snapshots are read-only audit references to the exact existing prompts. Do NOT load them wholesale as operational phase instructions: they also contain now-obsolete per-group and exactly-two-image restrictions.

## Phase mapping

01_groups: agent, source_integrity, original B1.1 narrative principles, groups memory.
02_planning: agent, ALL operational visual rules, beats/visuals memory, full authoritative script + materialized groups + avatar image.
03_evaluation: agent, ALL operational visual rules, beats/visuals memory, current whole-script plan + tool report.
04_correction: same immutable rules as evaluation, plus corrections and the complete latest plan.
05_delivery: agent, source_integrity, carrier and delivery rules, fresh validator report.

Real timestamps do not exist before TTS/STT. A rhythm map is expressed as relative positions in the script. Later image-generation motion refinement must consult rules/post_generation_motion.md and actual final PNGs.

## Important compatibility boundary

ProfessionalB2Output rejects extra fields. Projection places 2–4 image subshots in visual_plan, a strict-compatible B2 view in b2_merged and Haiku instructions in editor_plan; do not add image_prompts or editor_notes to the legacy B2 payload. Existing B1.1/B1.2/B2 execution remains active until an explicit runner integration replaces ONLY the Professional path.
