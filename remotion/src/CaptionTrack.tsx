import {Sequence, useCurrentFrame} from "remotion";

import type {RemotionCaptionCue} from "./types";

const CaptionCueView = ({
  cue,
  motionFrames,
}: {
  cue: RemotionCaptionCue;
  motionFrames: number;
}) => {
  const localFrame = useCurrentFrame();
  const duration = cue.end_frame - cue.start_frame;
  const effectiveMotionFrames = Math.min(
    motionFrames,
    Math.max(0, Math.floor((duration - 1) / 2)),
  );
  const enterProgress =
    effectiveMotionFrames === 0
      ? 1
      : Math.min(1, localFrame / effectiveMotionFrames);
  const remainingFrames = duration - 1 - localFrame;
  const exitProgress =
    effectiveMotionFrames === 0
      ? 1
      : Math.min(1, Math.max(0, remainingFrames) / effectiveMotionFrames);
  const motionProgress = Math.min(enterProgress, exitProgress);

  return (
    <div
      style={{
        position: "absolute",
        left: "12%",
        right: "12%",
        bottom: 84,
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
        opacity: 0.82 + motionProgress * 0.18,
        transform: `translateY(${(1 - motionProgress) * 10}px) scale(${0.99 + motionProgress * 0.01})`,
      }}
    >
      <div
        style={{
          maxWidth: "100%",
          padding: "22px 34px 24px",
          borderRadius: 26,
          backgroundColor: "rgba(0, 0, 0, 0.68)",
          boxShadow: "0 8px 28px rgba(0, 0, 0, 0.28)",
          color: "white",
          fontFamily: "Arial, Helvetica, sans-serif",
          fontSize: 64,
          fontWeight: 800,
          lineHeight: 1.12,
          letterSpacing: -1.0,
          textAlign: "center",
          textShadow: "0 2px 5px rgba(0, 0, 0, 0.72)",
        }}
      >
        {cue.words.map((word, index) => {
          const start = word.start_frame - cue.start_frame;
          const end = word.end_frame - cue.start_frame;
          const active = localFrame >= start && localFrame < end;

          return (
            <span
              key={word.word_id}
              style={{
                color: active ? "#ffd166" : "white",
              }}
            >
              {index > 0 ? " " : ""}
              {word.text}
            </span>
          );
        })}
      </div>
    </div>
  );
};

export const CaptionTrack = ({
  captions,
  motionFrames = 0,
}: {
  captions: RemotionCaptionCue[];
  motionFrames?: number;
}) => {
  return (
    <>
      {captions.map((cue) => (
        <Sequence
          key={cue.id}
          from={cue.start_frame}
          durationInFrames={cue.end_frame - cue.start_frame}
          name={`Caption ${cue.id}`}
        >
          <CaptionCueView cue={cue} motionFrames={motionFrames} />
        </Sequence>
      ))}
    </>
  );
};
