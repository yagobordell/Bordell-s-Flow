import {Video} from "@remotion/media";
import {AbsoluteFill, Sequence, staticFile} from "remotion";

import {CaptionTrack} from "./CaptionTrack";
import type {RemotionRenderProps} from "./types";

export const Phase9Visual = ({shots, captions, visual_profile: profile}: RemotionRenderProps) => {
  return (
    <AbsoluteFill style={{backgroundColor: "black"}}>
      {shots.map((shot) => (
        <Sequence
          key={shot.shot_id}
          from={shot.start_frame}
          durationInFrames={shot.duration_frames}
          name={`Shot ${shot.shot_id}`}
        >
          <Video
            src={staticFile(shot.src.replace(/^\//, ""))}
            muted
            objectFit="cover"
            style={{
              width: "100%",
              height: "100%",
            }}
          />
        </Sequence>
      ))}
      {profile.show_captions ? <CaptionTrack captions={captions} /> : null}
    </AbsoluteFill>
  );
};
