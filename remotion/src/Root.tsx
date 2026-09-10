import {Composition} from "remotion";

import {Phase9Visual} from "./Phase9Visual";
import type {RemotionRenderProps} from "./types";
import {validateRenderProps} from "./validation";

const defaultProps: RemotionRenderProps = {
  schema_version: "1",
  width: 768,
  height: 1280,
  fps: 24,
  total_frames: 1,
  shots: [],
  captions: [],
  presentation_normalizations: [],
};

export const RemotionRoot = () => {
  return (
    <Composition
      id="Phase9Visual"
      component={Phase9Visual}
      durationInFrames={1}
      fps={24}
      width={768}
      height={1280}
      defaultProps={defaultProps}
      calculateMetadata={({props}) => {
        validateRenderProps(props);
        return {
          durationInFrames: props.total_frames,
          fps: props.fps,
          width: props.width,
          height: props.height,
        };
      }}
    />
  );
};
