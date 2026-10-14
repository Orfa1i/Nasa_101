#!/usr/bin/env node
/**
 * Post-build checks on .next (run after `npm run build`):
 *  1. the first-load JavaScript of "/" stays within budget (gzip);
 *  2. the heavy map libraries (deck.gl/luma.gl, MapLibre GL, Mapbox GL) are code-split out of it;
 *  3. no credential-shaped strings were bundled into the static output.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';

const NEXT = new URL('../.next/', import.meta.url).pathname;
const BUDGET_GZIP_KB = 200;
const HEAVY = { 'deck.gl / luma.gl': /luma\.gl|deck\.gl/, 'MapLibre GL': /maplibre/, 'Mapbox GL': /mapbox-gl/, Recharts: /recharts/ };
const SECRETS = {
  'Anthropic key': /sk-ant-[A-Za-z0-9_-]{20,}/,
  'Mapbox token': /\b[ps]k\.eyJ[A-Za-z0-9_-]{20,}/,
  'AWS key': /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/,
  'GitHub token': /\bgh[pousr]_[A-Za-z0-9]{30,}\b/,
};

const failures = [];
const manifest = JSON.parse(readFileSync(join(NEXT, 'build-manifest.json'), 'utf8'));
const firstLoad = [...new Set([...(manifest.polyfillFiles ?? []), ...(manifest.pages['/_app'] ?? []), ...(manifest.pages['/'] ?? [])])].filter((f) => f.endsWith('.js'));
let gz = 0;
const firstLoadText = firstLoad.map((f) => {
  const buf = readFileSync(join(NEXT, f));
  gz += gzipSync(buf).length;
  return buf.toString('utf8');
});
const kb = gz / 1024;
console.log(`first-load JS for "/": ${firstLoad.length} files, ${kb.toFixed(1)} kB gzip (budget ${BUDGET_GZIP_KB} kB)`);
if (kb > BUDGET_GZIP_KB) failures.push(`first-load JS ${kb.toFixed(1)} kB gzip exceeds ${BUDGET_GZIP_KB} kB`);

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}
const allChunks = walk(join(NEXT, 'static')).filter((p) => p.endsWith('.js'));
for (const [name, re] of Object.entries(HEAVY)) {
  const inFirst = firstLoadText.some((t) => re.test(t));
  const lazy = allChunks.filter((p) => re.test(readFileSync(p, 'utf8'))).length;
  console.log(`${name}: ${inFirst ? 'IN FIRST LOAD' : 'code-split'} (${lazy} lazy chunk(s))`);
  if (inFirst) failures.push(`${name} is in the first-load bundle`);
  if (!lazy) failures.push(`${name} is missing from the build`);
}
for (const p of walk(join(NEXT, 'static'))) {
  if (!/\.(js|css|json|html)$/.test(p)) continue;
  const text = readFileSync(p, 'utf8');
  for (const [name, re] of Object.entries(SECRETS)) if (re.test(text)) failures.push(`${name} found in ${p}`);
}
console.log(`scanned ${allChunks.length} static JS chunks for credentials`);
if (failures.length) {
  console.error(`\nBundle check FAILED:\n - ${failures.join('\n - ')}`);
  process.exit(1);
}
console.log('Bundle check passed.');
