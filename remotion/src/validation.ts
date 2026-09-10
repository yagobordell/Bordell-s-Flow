import type {RemotionRenderProps} from "./types";

const isPositiveInteger = (value: number): boolean =>
  Number.isInteger(value) && value > 0;

const isNonNegativeInteger = (value: number): boolean =>
  Number.isInteger(value) && value >= 0;

export const validateRenderProps = (props: RemotionRenderProps): void => {
  if (props.schema_version !== "2") {
    throw new Error(`Unsupported Remotion props schema: ${props.schema_version}`);
  }
  if (
    !isPositiveInteger(props.width) ||
    !isPositiveInteger(props.height) ||
    !isPositiveInteger(props.fps) ||
    !isPositiveInteger(props.total_frames)
  ) {
    throw new Error("Remotion dimensions, fps and total_frames must be positive integers");
  }

  const profile = props.visual_profile;
  if (
    !isNonNegativeInteger(profile.transition_frames) ||
    !isNonNegativeInteger(profile.caption_motion_frames) ||
    !isNonNegativeInteger(profile.boundary_accent_frames)
  ) {
    throw new Error("Phase 9.4 visual frame windows must be non-negative integers");
  }
  if (
    profile.transition_floor_opacity < 0 ||
    profile.transition_floor_opacity > 1
  ) {
    throw new Error("Phase 9.4 transition floor opacity must be between 0 and 1");
  }
  if (profile.transition_scale < 1 || profile.transition_scale > 1.1) {
    throw new Error("Phase 9.4 transition scale must be between 1 and 1.1");
  }
  if (typeof profile.show_progress_bar !== "boolean") {
    throw new Error("Phase 9.4 progress bar flag must be boolean");
  }

  if (props.shots.length === 0) {
    if (
      props.total_frames !== 1 ||
      props.captions.length !== 0 ||
      props.presentation_normalizations.length !== 0
    ) {
      throw new Error("Only the one-frame Studio placeholder may omit shots");
    }
    return;
  }

  props.shots.forEach((shot, index) => {
    if (shot.shot_id !== index + 1) {
      throw new Error("Remotion shot IDs must be consecutive starting at 1");
    }
    if (shot.end_frame <= shot.start_frame) {
      throw new Error(`Shot ${shot.shot_id} has a non-positive frame interval`);
    }
    if (shot.duration_frames !== shot.end_frame - shot.start_frame) {
      throw new Error(`Shot ${shot.shot_id} duration does not match its frame interval`);
    }
    if (!shot.src.startsWith("/media/")) {
      throw new Error(`Shot ${shot.shot_id} source must be staged below /media/`);
    }
    if (profile.transition_frames * 2 > shot.duration_frames) {
      throw new Error(`Shot ${shot.shot_id} is too short for the visual transition window`);
    }
    if (profile.boundary_accent_frames > shot.duration_frames) {
      throw new Error(`Shot ${shot.shot_id} is too short for the boundary accent window`);
    }
    if (index === 0 && shot.start_frame !== 0) {
      throw new Error("Remotion shot timeline must start at frame 0");
    }
    if (index > 0 && props.shots[index - 1].end_frame !== shot.start_frame) {
      throw new Error("Remotion shots must be contiguous without gaps or overlaps");
    }
  });

  if (props.shots[props.shots.length - 1].end_frame !== props.total_frames) {
    throw new Error("Remotion total_frames must match the final shot boundary");
  }

  let expectedWordId = 1;
  props.captions.forEach((caption, index) => {
    if (caption.id !== index + 1) {
      throw new Error("Remotion caption IDs must be consecutive starting at 1");
    }
    if (caption.words.length === 0) {
      throw new Error(`Caption ${caption.id} must contain at least one word`);
    }
    if (caption.start_frame !== caption.words[0].start_frame) {
      throw new Error(`Caption ${caption.id} start must match its first word`);
    }
    if (caption.end_frame !== caption.words[caption.words.length - 1].end_frame) {
      throw new Error(`Caption ${caption.id} end must match its final word`);
    }
    if (caption.end_frame > props.total_frames) {
      throw new Error(`Caption ${caption.id} extends beyond the composition`);
    }
    if (caption.text !== caption.words.map((word) => word.text).join(" ")) {
      throw new Error(`Caption ${caption.id} text does not match its words`);
    }
    if (index > 0 && props.captions[index - 1].end_frame > caption.start_frame) {
      throw new Error("Remotion caption cues must not overlap");
    }

    caption.words.forEach((word, wordIndex) => {
      if (word.word_id !== expectedWordId) {
        throw new Error(`Expected narration word ${expectedWordId}, got ${word.word_id}`);
      }
      expectedWordId += 1;
      if (word.end_frame <= word.start_frame) {
        throw new Error(`Narration word ${word.word_id} has no visible frame interval`);
      }
      if (wordIndex > 0 && caption.words[wordIndex - 1].end_frame > word.start_frame) {
        throw new Error(`Caption ${caption.id} contains overlapping word intervals`);
      }
      if (word.text.includes("†")) {
        throw new Error(`Narration word ${word.word_id} still contains a dagger artifact`);
      }
    });
  });

  const normalizedIds = new Set<number>();
  props.presentation_normalizations.forEach((item) => {
    if (normalizedIds.has(item.word_id)) {
      throw new Error(`Word ${item.word_id} has duplicate presentation normalizations`);
    }
    normalizedIds.add(item.word_id);
    if (item.rule !== "remove_unicode_dagger_u2020") {
      throw new Error(`Unsupported presentation normalization rule: ${item.rule}`);
    }
    if (item.source_text === item.display_text || item.display_text.includes("†")) {
      throw new Error(`Invalid dagger normalization for word ${item.word_id}`);
    }
  });
};
