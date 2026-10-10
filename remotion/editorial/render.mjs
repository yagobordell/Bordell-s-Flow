import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {bundle} from '@remotion/bundler';
import {selectComposition, renderMedia} from '@remotion/renderer';
import {validateRenderPlan} from './validate.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
if (process.argv.length !== 4) {
  throw new Error('Usage: node render.mjs <render-plan.json> <overlay.mov>');
}
const plan = validateRenderPlan(JSON.parse(await fs.readFile(process.argv[2], 'utf8')));
const destination = path.resolve(process.argv[3]);
const props = {duration_frames: plan.duration_frames, events: plan.events};
const serveUrl = await bundle({entryPoint: path.join(root, 'src/index.ts')});
const composition = await selectComposition({
  serveUrl, id: 'EditorialOverlay', inputProps: props,
});
await renderMedia({
  composition, serveUrl, inputProps: props,
  codec: 'prores', proResProfile: '4444',
  imageFormat: 'png', pixelFormat: 'yuva444p10le',
  outputLocation: destination, concurrency: 2,
});
