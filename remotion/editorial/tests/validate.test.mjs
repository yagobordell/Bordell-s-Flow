import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validateRenderPlan} from '../validate.mjs';

const example = () => ({
  schema_version: 'editorial-render-v1', fps: 24, width: 1920,
  height: 1080, duration_frames: 240, events: [{
    id: 'e1-test', kind: 'progressive_text', start_frame: 50, end_frame: 110,
    elements: [{text: 'IMPORTANT', frame: 50}], animation: 'fade',
    layout: {
      x: 0.1, y: 0.3, width: 0.8, height: 0.2, font_size_px: 80,
      color: '#FFFFFF', stroke_color: '#111111', stroke_width_px: 2,
      align: 'center',
    },
  }],
});

test('accepts a bounded frame-accurate editorial plan', () => {
  assert.deepEqual(validateRenderPlan(example()).events.length, 1);
});
test('rejects overrun', () => {
  const value = example(); value.events[0].end_frame = 241;
  assert.throws(() => validateRenderPlan(value));
});
test('rejects escaping layout', () => {
  const value = example(); value.events[0].layout.x = 0.5;
  assert.throws(() => validateRenderPlan(value));
});
test('rejects duplicate effects', () => {
  const value = example(); value.events.push({...value.events[0]});
  assert.throws(() => validateRenderPlan(value));
});
