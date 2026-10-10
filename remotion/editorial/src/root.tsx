import React from 'react';
import {Composition} from 'remotion';
import {EditorialOverlay} from './timeline';
import type {RenderProps} from './timeline';

const defaults: RenderProps = {duration_frames: 1, events: []};

export const EditorialRoot: React.FC = () => (
  <Composition
    id="EditorialOverlay"
    component={EditorialOverlay}
    width={1920}
    height={1080}
    fps={24}
    durationInFrames={1}
    defaultProps={defaults}
    calculateMetadata={({props}) => ({
      durationInFrames: props.duration_frames,
      fps: 24,
      width: 1920,
      height: 1080,
    })}
  />
);
