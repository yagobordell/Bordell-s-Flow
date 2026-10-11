# Screens/documents — VERBATIM mandatory original B2 rule

The source block below is copied intact from src/ai_video_factory/bots/prompts/b2_professional.md. Apply it to EVERY image prompt generation, global review and correction, without abbreviation.

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
