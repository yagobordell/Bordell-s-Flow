export type RemotionShot = {
  shot_id: number;
  src: string;
  start_frame: number;
  end_frame: number;
  duration_frames: number;
};

export type RemotionCaptionWord = {
  word_id: number;
  text: string;
  start_frame: number;
  end_frame: number;
};

export type RemotionCaptionCue = {
  id: number;
  text: string;
  start_frame: number;
  end_frame: number;
  words: RemotionCaptionWord[];
};

export type PresentationNormalization = {
  word_id: number;
  source_text: string;
  display_text: string;
  rule: "remove_unicode_dagger_u2020";
};

export type RemotionVisualProfile = {
  transition_frames: number;
  transition_floor_opacity: number;
  transition_scale: number;
  caption_motion_frames: number;
  boundary_accent_frames: number;
  show_progress_bar: boolean;
};

export type RemotionRenderProps = {
  schema_version: "2";
  width: number;
  height: number;
  fps: number;
  total_frames: number;
  shots: RemotionShot[];
  captions: RemotionCaptionCue[];
  presentation_normalizations: PresentationNormalization[];
  visual_profile: RemotionVisualProfile;
};
