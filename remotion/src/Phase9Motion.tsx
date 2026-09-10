import {AbsoluteFill} from "remotion";

import {CaptionTrack} from "./CaptionTrack";
import {MotionOverlayTrack} from "./MotionOverlayTrack";
import {MotionShotTrack} from "./MotionShotTrack";
import type {RemotionRenderProps} from "./types";

export const Phase9Motion = ({
  shots,
  captions,
  total_frames: totalFrames,
  visual_profile: profile,
}: RemotionRenderProps) => {
  return (
    <AbsoluteFill style={{backgroundColor: "black"}}>
      <MotionShotTrack shots={shots} profile={profile} />
      <MotionOverlayTrack
        shots={shots}
        totalFrames={totalFrames}
        profile={profile}
      />
      <CaptionTrack
        captions={captions}
        motionFrames={profile.caption_motion_frames}
      />
    </AbsoluteFill>
  );
};
