# Phase 2 — ONE global beat + visual direction response

Decide the narrative segmentation and visual realization of the COMPLETE
original script TOGETHER in this SINGLE creative response. Read the entire
materialized B1.1 groups and narrative core before choosing any boundaries.

Each beat is an inseparable editorial decision: its exact source narration,
beat_type, observable on-screen action, real actor, visual carrier, prompt
assets, continuity/grid strategy and source-anchored editor notes. The same
director determines its natural YouTube pacing and its cinematographic
purpose simultaneously. There is NO separate beat-only model step and
NO separate visual-only model step.

Apply all loaded original B1.2 atomicity/semantic-boundary/cadence criteria
and hard <=100 stripped characters, <=40 words limits. Apply all loaded B2,
B2.1, real presenter identity, coherent video-motion, CTA/ebook, screen and
document, image-grid and Haiku rules to those SAME beats before responding.
The choice of cut must support the photographed action, continuity and
reveal; the choice of image must support its exact source narration.
No forced shot count or beats-per-minute rate.

Return a strict SparseDirection containing every original B1.1 group and
the complete ordered beats. Each beat MUST include beat_id, exact original
text, beat_type and visual_type. Carrier-specific fields ONLY:
- avatar: no prompt, audio or reference flags
- avatar_media: prompt describing supporting still
- media_image: image_prompts and generation_mode
- media_video: prompt and video_prompt
Never output end_anchor, occurrence, null prompt fields or empty metadata on
each beat. Put editor_notes, presenter_references and cta annotations ONCE
in the containing group; use empty lists there when not applicable.
The director chooses source cuts and visuals jointly in this same response.

The concatenated beats must preserve every original word and punctuation
mark in exact order, without invention or omission. Python restores source
whitespace from the original script, checks hard beat limits and verifies
the complete audiovisual result. Do not change the actual spoken text.
Any correction must carry the revised text AND its visual carrier together.

The whole plan is one phase output. No intermediate narrated-beat output
can be considered a successful result without its own visual choices.
