import {Sequence, useCurrentFrame} from "remotion";

import type {RemotionCaptionCue} from "./types";

const CaptionCueView = ({cue}: {cue: RemotionCaptionCue}) => {
  const localFrame = useCurrentFrame();

  return (
    <div
      style={{
        position: "absolute",
        left: "7%",
        right: "7%",
        bottom: 118,
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          maxWidth: "100%",
          padding: "18px 24px 20px",
          borderRadius: 22,
          backgroundColor: "rgba(0, 0, 0, 0.68)",
          boxShadow: "0 8px 28px rgba(0, 0, 0, 0.28)",
          color: "white",
          fontFamily: "Arial, Helvetica, sans-serif",
          fontSize: 54,
          fontWeight: 800,
          lineHeight: 1.12,
          letterSpacing: -1.1,
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

export const CaptionTrack = ({captions}: {captions: RemotionCaptionCue[]}) => {
  return (
    <>
      {captions.map((cue) => (
        <Sequence
          key={cue.id}
          from={cue.start_frame}
          durationInFrames={cue.end_frame - cue.start_frame}
          name={`Caption ${cue.id}`}
        >
          <CaptionCueView cue={cue} />
        </Sequence>
      ))}
    </>
  );
};
