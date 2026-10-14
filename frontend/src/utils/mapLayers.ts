/**
 * deck.gl layer construction for the map. Pure functions of the dashboard state, so they can be
 * unit-tested without WebGL (constructing a layer does not touch the GPU).
 */
import { COORDINATE_SYSTEM, WebMercatorViewport, type Layer } from '@deck.gl/core';
import { _TerrainExtension as TerrainExtension } from '@deck.gl/extensions';
import { TerrainLayer } from '@deck.gl/geo-layers';
import { BitmapLayer, GeoJsonLayer, ScatterplotLayer } from '@deck.gl/layers';
import { TerrainLoader } from '@loaders.gl/terrain';

import type { Alert, Facility, Matrix } from './api';
import { ALERT_COLORS, facilitiesGeoJson, type Bbox, type FacilityProps } from './facilities';
import { OVERLAY_BY_ID, renderMaskHatch, renderMatrix, type OverlayId, type RasterImage } from './overlays';

export { ALERT_COLORS, ALERT_LABELS, alertColorName, facilitiesGeoJson, facilityBbox } from './facilities';
export type { AlertColorName, Bbox, FacilityProps } from './facilities';

// --------------------------------------------------------------------------- //
// Camera
// --------------------------------------------------------------------------- //

export interface CameraTarget {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

/** Camera that frames `bbox` in a `width` x `height` viewport, tilted to show the terrain. */
export function cameraForBbox(bbox: Bbox, width: number, height: number, pitch = 50): CameraTarget {
  const w = Math.max(64, width);
  const h = Math.max(64, height);
  const padding = Math.max(8, Math.min(60, Math.round(Math.min(w, h) * 0.08)));
  const vp = new WebMercatorViewport({ width: w, height: h });
  const { longitude, latitude, zoom } = vp.fitBounds(
    [
      [bbox[0], bbox[1]],
      [bbox[2], bbox[3]],
    ],
    { padding },
  );
  // Tilting pushes the far edge away; back off a little so the whole bbox stays in view.
  return { longitude, latitude, zoom: Math.min(16, zoom - (pitch > 0 ? 0.35 : 0)), pitch, bearing: 0 };
}

export const WORLD_VIEW: CameraTarget = { longitude: -40, latitude: 10, zoom: 1.6, pitch: 0, bearing: 0 };

// --------------------------------------------------------------------------- //
// Terrain and basemap sources
// --------------------------------------------------------------------------- //

export interface TerrainSource {
  provider: 'mapbox' | 'open';
  elevationData: string;
  texture: string;
  elevationDecoder: { rScaler: number; gScaler: number; bScaler: number; offset: number };
  maxZoom: number;
  attribution: string;
}

/**
 * Mapbox terrain-RGB + satellite with a token; otherwise AWS Terrain Tiles (Terrarium encoding,
 * public, no key) textured with CARTO raster tiles.
 */
export function terrainSource(mapboxToken: string | undefined | null): TerrainSource {
  if (mapboxToken) {
    return {
      provider: 'mapbox',
      elevationData: `https://api.mapbox.com/v4/mapbox.terrain-rgb/{z}/{x}/{y}.png?access_token=${mapboxToken}`,
      texture: `https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}@2x.png?access_token=${mapboxToken}`,
      elevationDecoder: { rScaler: 6553.6, gScaler: 25.6, bScaler: 0.1, offset: -10000 },
      maxZoom: 15,
      attribution: '© Mapbox © OpenStreetMap © Maxar',
    };
  }
  return {
    provider: 'open',
    elevationData: 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png',
    texture: 'https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png',
    elevationDecoder: { rScaler: 256, gScaler: 1, bScaler: 1 / 256, offset: -32768 },
    maxZoom: 15,
    attribution: 'Terrain: Mapzen/AWS Terrain Tiles · Map tiles © CARTO © OpenStreetMap contributors',
  };
}

// --------------------------------------------------------------------------- //
// Raster images
// --------------------------------------------------------------------------- //

/** ImageData in the browser; a plain {data, width, height} texture source elsewhere (tests). */
export function toTextureSource(img: RasterImage): ImageData | RasterImage {
  if (typeof ImageData !== 'undefined') return new ImageData(img.data, img.width, img.height);
  return img;
}

// --------------------------------------------------------------------------- //
// Layer stack
// --------------------------------------------------------------------------- //

export interface LayerState {
  facilities: Facility[];
  latestAlerts: Record<string, Alert | null>;
  selectedId: string | null;
  activeOverlays: OverlayId[];
  matrices: Partial<Record<OverlayId, Matrix>>;
  mask: Matrix | null;
  showMask: boolean;
  overlayOpacity: number;
  terrain: boolean;
  terrainSource: TerrainSource;
  pickedPoint: [number, number] | null;
  onTerrainTile?: (ok: boolean) => void;
}

/** Draw order, bottom to top. */
export const OVERLAY_ORDER: OverlayId[] = ['coherence', 'spectral_rmse', 'flow_accumulation', 'displacement', 'sai', 'anomaly_regime'];

const imageCache = new WeakMap<Matrix, Map<string, { image: ImageData | RasterImage; maskedCells: number }>>();

function overlayImage(id: OverlayId, m: Matrix, mask: Matrix | null) {
  let byKey = imageCache.get(m);
  if (!byKey) imageCache.set(m, (byKey = new Map()));
  const key = `${id}:${mask ? mask.source.file + mask.shape.join('x') : 'none'}`;
  let entry = byKey.get(key);
  if (!entry) {
    const r = renderMatrix(m, OVERLAY_BY_ID[id], mask);
    entry = { image: toTextureSource(r), maskedCells: r.maskedCells };
    byKey.set(key, entry);
  }
  return entry;
}

const maskCache = new WeakMap<Matrix, ImageData | RasterImage>();

let geoJsonCache: { facilities: Facility[]; alerts: Record<string, Alert | null>; data: ReturnType<typeof facilitiesGeoJson> } | null = null;

function facilityFeatures(facilities: Facility[], alerts: Record<string, Alert | null>) {
  if (!geoJsonCache || geoJsonCache.facilities !== facilities || geoJsonCache.alerts !== alerts) {
    geoJsonCache = { facilities, alerts, data: facilitiesGeoJson(facilities, alerts) };
  }
  return geoJsonCache.data;
}

export function buildLayers(s: LayerState): Layer[] {
  const layers: Layer[] = [];
  const drape = s.terrain ? [new TerrainExtension()] : [];
  // A layer cannot swap its extensions in place, so draped and flat variants get distinct ids.
  const sfx = s.terrain ? '@draped' : '';

  if (s.terrain) {
    const t = s.terrainSource;
    layers.push(
      new TerrainLayer({
        id: 'terrain',
        minZoom: 0,
        maxZoom: t.maxZoom,
        elevationDecoder: t.elevationDecoder,
        elevationData: t.elevationData,
        texture: t.texture,
        meshMaxError: 4,
        color: [60, 64, 72],
        operation: 'terrain+draw',
        pickable: true,
        // Decode tiles on the main thread: deck.gl's default terrain loader is worker-only and
        // fetches its worker script from a CDN, which breaks behind firewalls / offline.
        loaders: [TerrainLoader],
        loadOptions: { worker: false },
        onTileLoad: () => s.onTerrainTile?.(true),
        onTileError: (err: unknown) => {
          console.warn('LithoCreep: terrain tile failed:', err instanceof Error ? err.message : err);
          s.onTerrainTile?.(false);
        },
      }),
    );
  }

  // Footprints go under the matrices, so a pick inside the processed area returns the matrix cell.
  const features = facilityFeatures(s.facilities, s.latestAlerts);
  layers.push(
    new GeoJsonLayer<FacilityProps>({
      id: `facilities${sfx}`,
      data: features,
      pickable: true,
      stroked: true,
      filled: true,
      getFillColor: (f) => [...ALERT_COLORS[f.properties.alert_color], f.properties.id === s.selectedId ? 25 : 70],
      getLineColor: (f) => [...ALERT_COLORS[f.properties.alert_color], 255],
      getLineWidth: (f) => (f.properties.id === s.selectedId ? 4 : 2),
      lineWidthUnits: 'pixels',
      lineWidthMinPixels: 2,
      updateTriggers: { getFillColor: s.selectedId, getLineWidth: s.selectedId },
      extensions: drape,
    }),
  );

  let drawnMatrices = 0;
  for (const id of OVERLAY_ORDER) {
    const m = s.matrices[id];
    if (!s.activeOverlays.includes(id) || !m) continue;
    const { image } = overlayImage(id, m, s.showMask ? s.mask : null);
    drawnMatrices++;
    layers.push(
      new BitmapLayer({
        id: `matrix-${id}${sfx}`,
        bounds: m.bounds,
        image,
        opacity: s.overlayOpacity,
        _imageCoordinateSystem: COORDINATE_SYSTEM.LNGLAT,
        textureParameters: { minFilter: 'nearest', magFilter: 'nearest' },
        pickable: true,
        extensions: drape,
      }),
    );
  }

  if (s.showMask && s.mask && drawnMatrices > 0) {
    let image = maskCache.get(s.mask);
    if (!image) {
      image = toTextureSource(renderMaskHatch(s.mask));
      maskCache.set(s.mask, image);
    }
    layers.push(
      new BitmapLayer({
        id: `observability-mask${sfx}`,
        bounds: s.mask.bounds,
        image,
        opacity: 0.9,
        _imageCoordinateSystem: COORDINATE_SYSTEM.LNGLAT,
        textureParameters: { minFilter: 'nearest', magFilter: 'nearest' },
        extensions: drape,
      }),
    );
  }

  // Footprints are sub-pixel at world zoom: also mark each facility centre, except the selected one.
  layers.push(
    new ScatterplotLayer<GeoJSON.Feature<GeoJSON.Polygon, FacilityProps>>({
      id: `facility-markers${sfx}`,
      data: features.features.filter((f) => f.properties.id !== s.selectedId),
      pickable: true,
      getPosition: (f) => {
        const ring = f.geometry.coordinates[0];
        return [(ring[0][0] + ring[2][0]) / 2, (ring[0][1] + ring[2][1]) / 2];
      },
      getRadius: 7,
      radiusUnits: 'pixels',
      stroked: true,
      getFillColor: (f) => [...ALERT_COLORS[f.properties.alert_color], 230],
      getLineColor: [15, 23, 42, 255],
      lineWidthMinPixels: 1.5,
      extensions: drape,
    }),
  );

  if (s.pickedPoint) {
    layers.push(
      new ScatterplotLayer<[number, number]>({
        id: `picked-point${sfx}`,
        data: [s.pickedPoint],
        getPosition: (d) => d,
        getRadius: 7,
        radiusUnits: 'pixels',
        stroked: true,
        filled: true,
        getFillColor: [255, 255, 255, 230],
        getLineColor: [15, 23, 42, 255],
        lineWidthMinPixels: 2,
        extensions: drape,
      }),
    );
  }
  return layers;
}
