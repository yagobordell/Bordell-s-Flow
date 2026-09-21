import {Composition} from "remotion";

import {Phase9Motion} from "./Phase9Motion";
import {Phase9Visual} from "./Phase9Visual";
import type {RemotionRenderProps} from "./types";
import {validateRenderProps} from "./validation";

const defaultProps: RemotionRenderProps = {
  schema_version: "2",
  width: 2560,
  height: 1440,
  fps: 24,
  total_frames: 1,
  shots: [],
  captions: [],
  presentation_normalizations: [],
  visual_profile: {
    transition_frames: 6,
    transition_floor_opacity: 0.72,
    transition_scale: 1.015,
    caption_motion_frames: 4,
    boundary_accent_frames: 5,
    show_progress_bar: true,
  },
};

const calculateMetadata = ({props}: {props: RemotionRenderProps}) => {
  validateRenderProps(props);
  return {
    durationInFrames: props.total_frames,
    fps: props.fps,
    width: props.width,
    height: props.height,
  };
};

export const RemotionRoot = () => {
  return (
    <>
      <Composition
        id="Phase9Visual"
        component={Phase9Visual}
        durationInFrames={1}
        fps={24}
        width={2560}
        height={1440}
        defaultProps={defaultProps}
        calculateMetadata={calculateMetadata}
      />
      <Composition
        id="Phase9Motion"
        component={Phase9Motion}
        durationInFrames={1}
        fps={24}
        width={2560}
        height={1440}
        defaultProps={defaultProps}
        calculateMetadata={calculateMetadata}
      />
    </>
  );
};
