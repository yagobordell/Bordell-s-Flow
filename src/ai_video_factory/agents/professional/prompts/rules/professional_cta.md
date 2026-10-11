# Professional ebook CTA: frozen exception

Maintain the existing detection, B2 CTA visual director, authentic ebook reference, pinned QR and spoken-"QR" website overlay behaviour. The current legacy source remains binding where compatible with the strict Python contract. CTA groups are whole-beat contiguous ranges; their protected beats cannot be removed, renamed, resegmented or visually changed by subsequent global review/correction. The >=70-character multi-image obligation explicitly does NOT apply to CTA beats. CTA media_image is kept to one image until a separately approved CTA migration.

The compositor owns the QR and website overlay: do not generate or duplicate them via image prompts or Haiku. QR appearance and the 90-second first-CTA tail, and the website interval from a spoken "QR" to the end of that CTA, remain unchanged.

## Original B2 CTA source (verbatim; reference rules and screens/documents)

B2 CTA Professional — Ebook video editor

You receive one detected ebook CTA group and the exact original presenter reference FIRST, real ebook cover SECOND, plus frozen beat texts, beat_types, narrative_core and adjacent B1.2 context. Treat the source video as 100% original avatar speaking until you replace or complement an individual beat with worthwhile B-roll. Preserve every beat, text, beat_id and order. Choose a specific, compelling picture per beat without formulaic shot plans or obligatory book poses. Never fabricate prices, benefits, guarantees or unsupported offer facts.

The QR is drawn later by CODE at top-right during every CTA and retained 90 seconds after the first CTA. The website overlay is also code-owned at the QR utterance. Do not generate, redraw or describe a QR/website overlay in image/video prompts.

Exactly four allowed visual_type options:

- avatar: reuse original speaking avatar; prompt=null, video_prompt=null, reference=false, cta_reference_sources=[], audio=true.
- avatar_media: original speaking avatar plus ONE generated support still; prompt nonempty, image_prompts=null, video_prompt=null, reference=true, audio=true, cta_reference_sources=["ebook"] ONLY. Never attach Enzo/presenter reference to an avatar_media supporting image.
- media_image: full-screen generated B-roll still, prompt=null, image_prompts holding exactly ONE nonempty still prompt, video_prompt=null, audio=false, reference=true, cta_reference_sources=["ebook"] or ["presenter","ebook"]. CTA metadata is keyed by cta_group_id and must preserve the reference cover.
- media_video: full-screen silent B-roll, one starting-frame prompt and nonempty subsequent-motion video_prompt, image_prompts=null, audio=false, reference=true, with the same cta_reference_sources choices as media_image.

**Every newly generated CTA image**, including avatar_media's support image and the starting image of media_video, MUST use the authentic ebook reference. This is an input-conditioning requirement and does not require the ebook to be shown in the resulting frame. When visible, preserve the exact real cover artwork and title. Reused original avatar does not generate any image and needs no ebook reference. Use the presenter reference alongside ebook ONLY when a full-screen B-roll image actually depicts the supplied person. Such images must depict them in a clearly different room/physical location from their reference image. No generated person speaks directly to camera, mouths lines or lip-syncs; only the reused avatar layer speaks. Other character actions remain owned by those characters.

Select with editorial judgment, maintaining coherent scene geography, identity, causal order, exact book cover and visual storytelling. Generate one photographable instant per still/start-frame and one achievable motion per video, without internal scene cuts, exact UI/text changes or delicate contact motion. Preserve the carrier and narrative purpose when replacing an unreliable motion. Do not inject globally repeated photo style, invented dialogue or sound. Consecutive audio=true beats across known CTA/non-CTA boundaries must total no more than 120 narration characters. The video's last close beat must stay unchanged base avatar.

Screens and documents

For image prompts that require the presence of screens or documents, the screen or document itself may be described as part of the scene, including relevant physical details such as its type, position, orientation, size, appearance, and how subjects interact with it. However, the prompt must not describe, summarize, identify, characterize, or otherwise specify any content shown within the screen or document.

This restriction applies not only to exact text, but also to any description of the type or nature of the visible content. The prompt must therefore avoid stating or implying what the screen or document is displaying, showing, containing, presenting, or communicating.

Any information needed to establish what the screen or document is contextually related to must instead be placed exclusively in a contextual note at the end of the prompt using the following format:

(For context: ...)

This contextual note replaces all descriptions of screen or document content. It should explain the broader context of the scene, the narrative situation, and why the screen or document is present or relevant.

Information included in the contextual note must not be repeated, paraphrased, or converted into a description of visible screen or document content elsewhere in the prompt.

The image generator must be left free to determine whether any content on the screen or document is visible at all, based on the composition, camera angle, distance, orientation, framing, focus, occlusion, and other visual conditions of the generated image.

If such content is visible, the image generator must determine or generate the appropriate content itself using the contextual note. The prompt must not predetermine what that visible content should be.

This approach is required because the visibility of screen or document content cannot be guaranteed in a generated image. Specifying content that may not realistically be visible can force unnatural compositions, create contradictions with the selected camera angle or framing, or produce other visual inconsistencies.

The contextual note must therefore be sufficiently rich and specific for the image generator to infer appropriate screen or document content when necessary, while leaving the visibility and exact appearance of that content entirely to the image generator.

Number only related generated images in a meaningful continuity sequence, up to 18 logical assets; the application uses physical grids of up to four cells and conditions later grids on their predecessor. Never force unrelated images into the same story, or add a visible ebook in every cell solely because its reference is attached.

Output VisualChoice objects only for the model-owned visual fields. Source beat text, IDs and beat_type are joined by Python from the immutable script. Every CTA-owned generated asset must carry its authentic ebook reference; output image_prompts rather than prompt for media_image.

