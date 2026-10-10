// Dependency-free preflight for the Remotion renderer. Never trust model output.
const hex = /^#[0-9a-fA-F]{6}$/;
const kinds = new Set([
  'progressive_text', 'number_emphasis', 'accumulating_list', 'text_card',
  'comparison_cards', 'process_flow',
]);
const animations = new Set(['fade', 'pop', 'slide']);
export const validateRenderPlan = (plan) => {
  if (!plan || plan.schema_version !== 'editorial-render-v1'
      || plan.fps !== 24 || plan.width !== 1920 || plan.height !== 1080
      || !Number.isInteger(plan.duration_frames) || plan.duration_frames < 1
      || !Array.isArray(plan.events)) {
    throw new Error('Invalid render plan metadata');
  }
  const seen = new Set();
  for (const event of plan.events) {
    if (typeof event.id !== 'string' || seen.has(event.id)
        || !Number.isInteger(event.start_frame) || !Number.isInteger(event.end_frame)
        || event.start_frame < 0 || event.end_frame <= event.start_frame
        || event.end_frame > plan.duration_frames) {
      throw new Error('Invalid editorial event');
    }
    seen.add(event.id);
    if (!kinds.has(event.kind) || !animations.has(event.animation)
        || !Array.isArray(event.elements)
        || event.elements.length < 1 || event.elements.length > 8) {
      throw new Error('Invalid editorial content or effect type');
    }
    if ((event.kind === 'process_flow' || event.kind === 'comparison_cards')
        && event.elements.length < 2) {
      throw new Error('Graphical editing requires multiple nodes');
    }
    if ((event.kind === 'text_card' || event.kind === 'number_emphasis')
        && event.elements.length !== 1) {
      throw new Error('Single-value effect has multiple elements');
    }
    for (const item of event.elements) {
      if (typeof item.text !== 'string' || item.text.length > 100 || !item.text.trim()
          || !Number.isInteger(item.frame) || item.frame < event.start_frame
          || item.frame >= event.end_frame) {
        throw new Error('Invalid editorial element');
      }
    }
    const box = event.layout;
    if (!box || ![box.x, box.y, box.width, box.height].every(Number.isFinite)
        || box.x < 0 || box.y < 0 || box.width <= 0 || box.height <= 0
        || box.x + box.width > 1.000001 || box.y + box.height > 1.000001
        || !Number.isInteger(box.font_size_px) || box.font_size_px < 20
        || box.font_size_px > 200 || !hex.test(box.color)
        || !hex.test(box.stroke_color)) {
      throw new Error('Invalid editorial geometry');
    }
  }
  return plan;
};
