import { deflateSync, inflateSync } from 'node:zlib';

import { expect, type Page } from '@playwright/test';

// --------------------------------------------------------------------------- //
// Minimal PNG encoder (RGBA, no filtering) for synthetic terrain tiles
// --------------------------------------------------------------------------- //

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf: Buffer): number {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

export function encodePng(width: number, height: number, rgba: (x: number, y: number) => [number, number, number, number]): Buffer {
  const raw = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (width * 4 + 1)] = 0;
    for (let x = 0; x < width; x++) {
      const [r, g, b, a] = rgba(x, y);
      const i = y * (width * 4 + 1) + 1 + x * 4;
      raw[i] = r;
      raw[i + 1] = g;
      raw[i + 2] = b;
      raw[i + 3] = a;
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}

/** Terrarium tile: elevation = 900 m + a 120 m ridge pattern (so the mesh is visibly 3D). */
function terrariumTile(): Buffer {
  return encodePng(256, 256, (x, y) => {
    const h = 900 + 120 * Math.sin((x / 256) * Math.PI) * Math.cos((y / 256) * Math.PI * 0.5);
    const v = h + 32768;
    return [Math.floor(v / 256), Math.floor(v) % 256, Math.floor((v % 1) * 256), 255];
  });
}

const TERRAIN_TILE = terrariumTile();
const TEXTURE_TILE = encodePng(256, 256, (x, y) => ((x >> 5) + (y >> 5)) % 2 ? [70, 78, 92, 255] : [88, 96, 110, 255]);

export interface NetworkLog {
  blocked: string[];
  terrainTiles: number;
}

/**
 * Keep the tests offline: same-origin requests pass; terrain/texture tiles are answered with
 * synthetic PNGs when `terrain` is true (aborted otherwise); everything else is aborted.
 */
export async function isolateNetwork(page: Page, { terrain }: { terrain: boolean }): Promise<NetworkLog> {
  const log: NetworkLog = { blocked: [], terrainTiles: 0 };
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') return route.continue();
    if (terrain && url.pathname.includes('/terrarium/')) {
      log.terrainTiles++;
      return route.fulfill({ status: 200, contentType: 'image/png', body: TERRAIN_TILE });
    }
    if (terrain && url.hostname === 'basemaps.cartocdn.com' && url.pathname.startsWith('/rastertiles/')) {
      return route.fulfill({ status: 200, contentType: 'image/png', body: TEXTURE_TILE });
    }
    log.blocked.push(url.hostname);
    return route.abort();
  });
  return log;
}

/** Collect uncaught page errors (console noise from deliberately blocked tiles is ignored). */
export function collectPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  return errors;
}

// --------------------------------------------------------------------------- //
// Map helpers
// --------------------------------------------------------------------------- //

export const SIERRITA = { bbox: [-111.12, 31.83, -111.04, 31.9] as const, centre: [-111.08, 31.865] as const };
export const BINGHAM = { centre: [-112.15, 40.525] as const };

export async function mapAttr(page: Page, name: string): Promise<string> {
  return (await page.getByTestId('map-viewer').getAttribute(`data-${name}`)) ?? '';
}

export async function layerIds(page: Page): Promise<string[]> {
  return (await mapAttr(page, 'layers')).split(',').filter(Boolean);
}

/** Wait until the camera has settled near lon/lat (fly-to transitions take ~2 s). */
export async function waitForCamera(page: Page, lon: number, lat: number, { pitch, tol = 0.01 }: { pitch?: number; tol?: number } = {}) {
  await expect
    .poll(
      async () => {
        const a = Number(await mapAttr(page, 'longitude'));
        const b = Number(await mapAttr(page, 'latitude'));
        const p = Number(await mapAttr(page, 'pitch'));
        return Math.abs(a - lon) < tol && Math.abs(b - lat) < tol && (pitch === undefined || Math.abs(p - pitch) < 0.5);
      },
      { timeout: 20_000 },
    )
    .toBe(true);
  // let the last transition frame and tile loads land
  await page.waitForTimeout(400);
}

/** Screen position of lon/lat for a pitch-0, bearing-0 Web Mercator view (deck.gl tile size 512). */
export async function screenPoint(page: Page, lon: number, lat: number): Promise<{ x: number; y: number }> {
  const box = (await page.getByTestId('map-viewer').boundingBox())!;
  const zoom = Number(await mapAttr(page, 'zoom'));
  const cLon = Number(await mapAttr(page, 'longitude'));
  const cLat = Number(await mapAttr(page, 'latitude'));
  expect(Number(await mapAttr(page, 'pitch'))).toBeLessThan(0.5);
  const world = 512 * 2 ** zoom;
  const px = (lo: number) => ((lo + 180) / 360) * world;
  const py = (la: number) => ((1 - Math.log(Math.tan(Math.PI / 4 + (la * Math.PI) / 360)) / Math.PI) / 2) * world;
  return { x: box.x + box.width / 2 + (px(lon) - px(cLon)), y: box.y + box.height / 2 + (py(lat) - py(cLat)) };
}

export interface DecodedImage {
  width: number;
  height: number;
  /** RGBA bytes */
  data: Uint8Array;
}

/** Decode an 8-bit RGB/RGBA, non-interlaced PNG (what Playwright screenshots produce). */
export function decodePng(png: Buffer): DecodedImage {
  let pos = 8;
  let width = 0;
  let height = 0;
  let colorType = 6;
  const idat: Buffer[] = [];
  while (pos < png.length) {
    const len = png.readUInt32BE(pos);
    const type = png.toString('ascii', pos + 4, pos + 8);
    const data = png.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      if (data[8] !== 8 || data[12] !== 0) throw new Error('only 8-bit non-interlaced PNGs are supported');
      colorType = data[9];
    } else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    pos += 12 + len;
  }
  const bpp = colorType === 6 ? 4 : colorType === 2 ? 3 : 0;
  if (!bpp) throw new Error(`unsupported PNG colour type ${colorType}`);
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * bpp;
  const out = new Uint8Array(width * height * 4);
  let prev = new Uint8Array(stride);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const line = new Uint8Array(raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1)));
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? line[i - bpp] : 0;
      const b = prev[i];
      const c = i >= bpp ? prev[i - bpp] : 0;
      let add = 0;
      if (filter === 1) add = a;
      else if (filter === 2) add = b;
      else if (filter === 3) add = (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        add = pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      line[i] = (line[i] + add) & 0xff;
    }
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * 4;
      out[o] = line[x * bpp];
      out[o + 1] = line[x * bpp + 1];
      out[o + 2] = line[x * bpp + 2];
      out[o + 3] = bpp === 4 ? line[x * bpp + 3] : 255;
    }
    prev = line;
  }
  return { width, height, data: out };
}

/** Mean absolute per-channel difference (0-255) between two same-size screenshots. */
export function meanDifference(a: Buffer, b: Buffer): number {
  const x = decodePng(a);
  const y = decodePng(b);
  if (x.width !== y.width || x.height !== y.height) throw new Error('screenshots differ in size');
  let sum = 0;
  for (let i = 0; i < x.data.length; i += 4) {
    sum += Math.abs(x.data[i] - y.data[i]) + Math.abs(x.data[i + 1] - y.data[i + 1]) + Math.abs(x.data[i + 2] - y.data[i + 2]);
  }
  return sum / ((x.data.length / 4) * 3);
}


/** Fraction of pixels matching an RGB predicate (e.g. "clearly green"), robust to terrain shading. */
export function pixelFraction(png: Buffer, pred: (r: number, g: number, b: number) => boolean): number {
  const img = decodePng(png);
  let hits = 0;
  for (let i = 0; i < img.data.length; i += 4) if (pred(img.data[i], img.data[i + 1], img.data[i + 2])) hits++;
  return hits / (img.data.length / 4);
}

/** Zoom the (pitch-0) map in around lon/lat with the mouse wheel until `zoom >= minZoom`. */
export async function zoomInAt(page: Page, lon: number, lat: number, minZoom: number) {
  for (let i = 0; i < 12 && Number(await mapAttr(page, 'zoom')) < minZoom; i++) {
    const p = await screenPoint(page, lon, lat);
    await page.mouse.move(p.x, p.y);
    await page.mouse.wheel(0, -400);
    let last = '';
    await expect
      .poll(async () => {
        const z = await mapAttr(page, 'zoom');
        const settled = z === last;
        last = z;
        return settled;
      }, { intervals: [150] })
      .toBe(true);
  }
  expect(Number(await mapAttr(page, 'zoom'))).toBeGreaterThanOrEqual(minZoom);
}
