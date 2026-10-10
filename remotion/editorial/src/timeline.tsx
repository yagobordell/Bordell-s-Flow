import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame} from 'remotion';

type Element = {text: string; frame: number};
type Layout = {
  x: number; y: number; width: number; height: number;
  font_size_px: number; color: string; stroke_color: string;
  stroke_width_px: number; align: 'left' | 'center' | 'right';
};
type EditorialEvent = {
  id: string;
  kind: 'progressive_text' | 'number_emphasis' | 'accumulating_list' | 'text_card';
  start_frame: number;
  end_frame: number;
  elements: Element[];
  animation: 'fade' | 'pop' | 'slide';
  layout: Layout;
};
export type RenderProps = {duration_frames: number; events: EditorialEvent[]};

const revealStyle = (frame: number, appearance: number, animation: string) => {
  const progress = interpolate(frame, [appearance, appearance + 8], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return {
    opacity: progress,
    transform: animation === 'pop'
      ? 'scale(' + (0.86 + progress * 0.14) + ')'
      : animation === 'slide' ? 'translateY(' + (18 * (1 - progress)) + 'px)'
        : 'none',
  };
};

export const EditorialOverlay: React.FC<RenderProps> = ({events}) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{backgroundColor: 'transparent'}}>
      {events.filter((event) =>
        frame >= event.start_frame && frame < event.end_frame
      ).map((event) => {
        const area = event.layout;
        const list = event.kind === 'accumulating_list';
        const items = event.elements.filter((el) => frame >= el.frame);
        const isProgressive = event.kind === 'progressive_text';
        const ending = interpolate(
          frame, [event.end_frame - 6, event.end_frame], [1, 0],
          {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}
        );
        return (
          <div key={event.id} style={{
            position: 'absolute',
            left: (area.x * 100) + '%',
            top: (area.y * 100) + '%',
            width: (area.width * 100) + '%',
            height: (area.height * 100) + '%',
            boxSizing: 'border-box',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            alignItems: area.align === 'left' ? 'flex-start'
              : area.align === 'right' ? 'flex-end' : 'center',
            gap: list ? 12 : 0,
            opacity: ending,
            overflow: 'hidden',
            padding: 8,
          }}>
            {isProgressive ? (
              <div style={{
                color: area.color, fontSize: area.font_size_px, fontWeight: 800,
                fontFamily: 'Arial, sans-serif', lineHeight: 1.08,
                textAlign: area.align, maxWidth: '100%',
                WebkitTextStroke: area.stroke_width_px + 'px ' + area.stroke_color,
                paintOrder: 'stroke fill', overflowWrap: 'break-word',
              }}>
                {items.map((item, index) => (
                  <span key={index} style={{
                    ...revealStyle(frame, item.frame, event.animation),
                    display: 'inline-block', marginRight: '0.23em',
                  }}>{item.text}</span>
                ))}
              </div>
            ) : items.map((el, index) => {
              const appearance = list ? el.frame : event.start_frame;
              return (
                <div key={index} style={{
                  ...revealStyle(frame, appearance, event.animation),
                  color: area.color,
                  fontSize: area.font_size_px,
                  fontWeight: 800,
                  fontFamily: 'Arial, sans-serif',
                  textAlign: area.align,
                  WebkitTextStroke: area.stroke_width_px + 'px ' + area.stroke_color,
                  paintOrder: 'stroke fill',
                  lineHeight: 1.08,
                  maxWidth: '100%',
                  wordBreak: 'normal',
                  overflowWrap: 'break-word',
                  whiteSpace: 'normal',
                  backgroundColor: event.kind === 'text_card'
                    ? 'rgba(0, 0, 0, 0.55)' : 'transparent',
                  padding: event.kind === 'text_card' ? 18 : 0,
                  borderRadius: event.kind === 'text_card' ? 12 : 0,
                }}>
                  {list ? (index + 1) + '. ' : ''}{el.text}
                </div>
              );
            })}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
