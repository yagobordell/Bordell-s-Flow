# Image continuity, still subshots, and grids

Numbering and logical continuity

Choose a logical multi-beat or within-beat continuous run of 2–18 generated still assets inside one narrative block, using generation_mode=grid on EACH contributing media_image beat. A beat with exactly one still can join its adjacent still-image beats in the same continuity run. PYTHON assigns the consecutive 1., 2., ... numbering across the ENTIRE run when projecting the accepted plan; the model supplies clean unnumbered scene prompts. A second still within one beat counts as an additional generated asset in the same chronological chain. The runtime packs logical runs into physical grids of at most FOUR cells and conditions each later grid on its predecessor. This physical split does not break the story's continuity. Respect CTA boundaries and natural scene changes. Do not demand a grid when independent scenes work better. Avatar_media may only join grids of the same panel format; do not mix it into a full-frame B-roll grid.

## New approved subshot contract

The existing B2.1 "exactly two" image rule is upgraded in the NEW Professional agent only: media_image beats of 70 or more characters must have **at least two** distinct prompts, up to FOUR. Other media_image beats may have 1–4. Never create multiple images for avatar, avatar_media or media_video; extra images are reserved for media_image. Multi-image beats keep one beat_id and one narration interval; after STT, Python divides that interval into nearly equal integer-frame durations with any remainder distributed deterministically. No seconds are guessed in preproduction.

CTA beats retain their original exemption from the >=70 split rule and their protected single-image asset behaviour. Within a coherent numbered continuity group, physical grids contain 2–4 panels and can form an 18-asset logical chain. Keep chronological actor/object/state continuity without forcing unrelated scenes into grids. Check full grid feasibility before paid generation.
