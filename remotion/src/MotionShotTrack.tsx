import {Video} from "@remotion/media";
import {AbsoluteFill, Sequence, staticFile, useCurrentFrame} from "remotion";

import type {RemotionShot, RemotionVisualProfile} from "./types";

const MotionShot = ({
  shot,
  index,
  shotCount,
  profile,
}: {
  shot: RemotionShot;
  index: number;
  shotCount: number;
  profile: RemotionVisualProfile;
}) => {
  const localFrame = useCurrentFrame();
  const transitionFrames = Math.min(
    profile.transition_frames,
    Math.floor(shot.duration_frames / 2),
  );

  const entryProgress =
    index === 0 || transitionFrames === 0
      ? 1
      : Math.min(1, localFrame / transitionFrames);
  const remainingFrames = shot.duration_frames - 1 - localFrame;
  const exitProgress =
    index === shotCount - 1 || transitionFrames === 0
      ? 1
      : Math.min(1, Math.max(0, remainingFrames) / transitionFrames);

  const opacityProgress = Math.min(entryProgress, exitProgress);
  const opacity =
    profile.transition_floor_opacity +
    (1 - profile.transition_floor_opacity) * opacityProgress;

  const entryScale =
    1 + (profile.transition_scale - 1) * (1 - entryProgress);
  const exitScale = 1 + (profile.transition_scale - 1) * (1 - exitProgress);
  const scale = Math.max(entryScale, exitScale);

  return (
    <AbsoluteFill style={{backgroundColor: "black", overflow: "hidden"}}>
      <Video
        src={staticFile(shot.src.replace(/^\//, ""))}
        muted
        objectFit="cover"
        style={{
          width: "100%",
          height: "100%",
          opacity,
          transform: `scale(${scale})`,
        }}
      />
    </AbsoluteFill>
  );
};

export const MotionShotTrack = ({
  shots,
  profile,
}: {
  shots: RemotionShot[];
  profile: RemotionVisualProfile;
}) => {
  return (
    <>
      {shots.map((shot, index) => (
        <Sequence
          key={shot.shot_id}
          from={shot.start_frame}
          durationInFrames={shot.duration_frames}
          name={`Motion shot ${shot.shot_id}`}
        >
          <MotionShot
            shot={shot}
            index={index}
            shotCount={shots.length}
            profile={profile}
          />
        </Sequence>
      ))}
    </>
  );
};
