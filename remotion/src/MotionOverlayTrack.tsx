import {AbsoluteFill, useCurrentFrame} from "remotion";

import type {RemotionShot, RemotionVisualProfile} from "./types";

const BoundaryAccent = ({
  shots,
  frames,
}: {
  shots: RemotionShot[];
  frames: number;
}) => {
  const frame = useCurrentFrame();
  if (frames === 0) {
    return null;
  }

  const activeShot = shots.find(
    (shot) =>
      shot.shot_id > 1 &&
      frame >= shot.start_frame &&
      frame < shot.start_frame + frames,
  );
  if (!activeShot) {
    return null;
  }

  const localFrame = frame - activeShot.start_frame;
  const progress = frames === 1 ? 0.5 : localFrame / (frames - 1);
  const leftPercent = -20 + progress * 140;
  const opacity = frames === 1 ? 0.16 : Math.sin(Math.PI * progress) * 0.18;

  return (
    <AbsoluteFill style={{overflow: "hidden", pointerEvents: "none"}}>
      <div
        style={{
          position: "absolute",
          top: "-10%",
          bottom: "-10%",
          left: `${leftPercent}%`,
          width: "12%",
          transform: "skewX(-12deg)",
          background:
            "linear-gradient(90deg, rgba(255,255,255,0), rgba(255,255,255,0.85), rgba(255,255,255,0))",
          filter: "blur(18px)",
          opacity,
        }}
      />
    </AbsoluteFill>
  );
};

const ProgressBar = ({totalFrames}: {totalFrames: number}) => {
  const frame = useCurrentFrame();
  const progress = Math.min(1, (frame + 1) / totalFrames);

  return (
    <div
      style={{
        position: "absolute",
        top: 42,
        left: 48,
        right: 48,
        height: 5,
        borderRadius: 999,
        overflow: "hidden",
        backgroundColor: "rgba(255, 255, 255, 0.18)",
        boxShadow: "0 1px 6px rgba(0, 0, 0, 0.24)",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          height: "100%",
          width: `${progress * 100}%`,
          borderRadius: 999,
          backgroundColor: "rgba(255, 255, 255, 0.9)",
        }}
      />
    </div>
  );
};

export const MotionOverlayTrack = ({
  shots,
  totalFrames,
  profile,
}: {
  shots: RemotionShot[];
  totalFrames: number;
  profile: RemotionVisualProfile;
}) => {
  return (
    <AbsoluteFill style={{pointerEvents: "none"}}>
      <BoundaryAccent shots={shots} frames={profile.boundary_accent_frames} />
      {profile.show_progress_bar ? <ProgressBar totalFrames={totalFrames} /> : null}
    </AbsoluteFill>
  );
};
