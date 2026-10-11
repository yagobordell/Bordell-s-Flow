# Phase 2 — One global beats + visuals + editor planning pass

Work on the ENTIRE authoritative original script in ONE global creative task, using the materialized narrative groups from phase 1 as boundaries and global context, never calling the model separately per group. Segment complete source into beats by contiguous verbatim substrings; one independent and visually/narratively useful moment per beat, not arbitrary short fragments. Beats within a group have consecutive IDs (1A, 1B, ..., 1AA). <=40 words and <=100 stripped characters per beat. Preserve every original source character in sequence.

For each beat select visual_type, beat_type, audio, reference and generation-ready English prompts consistent with immutable rules. media_image MUST use image_prompts (1–4). From 70 characters inclusive, non-CTA media_image MUST have AT LEAST TWO distinct image prompts. Multi-image beats share ONE narration beat: their images are shown sequentially for equal frame allocations once STT duration is available. Use generation_mode=grid for coherent grouped image generation and independent otherwise. CTA-owned beats remain protected and use the existing single-image semantics. avatar_media supports only one accompanying still and media_video has one starting image plus a separate one-take video_prompt.

Use visual_type=avatar on STRICTLY FEWER THAN 45% of beats. avatar_media is excluded from this ratio but included in audio=true runs; each whole-video speaking run is <=120 characters across group boundaries. Do not inflate the beat count merely to reduce the ratio. The last beat of a close block is the untouched original avatar.

Add editor_notes for Haiku separately from image/video prompts. For word-triggered effects, include an exact source phrase AND its 1-based occurrence in that beat. No made-up seconds; STT resolves time later. Never embed title/overlay graphics in generation prompts.

Submit the complete plan and inspect the Python report and rhythm map; the runtime decides when evaluation begins.
