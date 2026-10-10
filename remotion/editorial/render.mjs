import fs from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {bundle} from '@remotion/bundler';
import {selectComposition, renderMedia} from '@remotion/renderer';
import {validateRenderPlan} from './validate.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
if (process.argv.length !== 4) {
  throw new Error('Usage: node render.mjs <render-plan.json> <layers-directory>');
}
const plan = validateRenderPlan(JSON.parse(await fs.readFile(process.argv[2], 'utf8')));
const directory = path.resolve(process.argv[3]);
await fs.mkdir(directory, {recursive: true});
const serveUrl = await bundle({entryPoint: path.join(root, 'src/index.ts')});
const clips = [];
for (let i = 0; i < plan.events.length; i++) {
  const event = plan.events[i];
  const inputProps = {duration_frames: plan.duration_frames, events: [event]};
  const composition = await selectComposition({
    serveUrl, id: 'EditorialOverlay', inputProps,
  });
  const basename = 'layer-' + String(i).padStart(3, '0') + '.mov';
  const location = path.join(directory, basename);
  await renderMedia({
    composition, serveUrl, inputProps,
    codec: 'prores', proResProfile: '4444',
    imageFormat: 'png', pixelFormat: 'yuva444p10le',
    outputLocation: location, concurrency: 2,
    frameRange: [event.start_frame, event.end_frame - 1],
  });
  clips.push({
    file: basename, start_frame: event.start_frame,
    end_frame: event.end_frame, id: event.id,
    sha256: createHash('sha256').update(await fs.readFile(location)).digest('hex'),
  });
}
await fs.writeFile(
  path.join(directory, 'layers.json'),
  JSON.stringify({schema_version: 'editorial-layers-v1', clips}, null, 2) + '\n',
);
