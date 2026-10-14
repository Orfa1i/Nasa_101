import { ALERT_COLORS, alertColorName, facilitiesGeoJson } from '@/utils/facilities';
import { OVERLAY_ORDER, buildLayers, cameraForBbox, terrainSource, type LayerState } from '@/utils/mapLayers';
import type { Matrix } from '@/utils/api';
import type { OverlayId } from '@/utils/overlays';
import { formatDate, normaliseTime, operaEpochs, provenance } from '@/utils/provenance';

import { DISP_TIMES, DSWX_TIMES, FACILITIES, MATRICES, REAL_DISP, SIERRITA_ALERT, SIERRITA_BBOX, SYNTH, layerInfo } from './fixtures';

describe('provenance watermark', () => {
  it('labels real observations authentic', () => {
    const p = provenance([REAL_DISP], null);
    expect(p.state).toBe('authentic');
    expect(p.headline).toBe('AUTHENTIC SATELLITE OBSERVATION');
    expect(p.sources).toEqual([{ label: 'OPERA_L3_DISP-S1_V1', synthetic: false, products: ['kinematics'] }]);
  });

  it('marks the whole view simulated when any shown source is synthetic', () => {
    const p = provenance([REAL_DISP, SYNTH], null);
    expect(p.state).toBe('simulated');
    expect(p.sources.map((s) => s.synthetic).sort()).toEqual([false, true]);
  });

  it('treats an alert built from synthetic inputs as simulated even with real matrices shown', () => {
    expect(provenance([REAL_DISP], SIERRITA_ALERT).state).toBe('simulated');
    expect(provenance([], SIERRITA_ALERT).state).toBe('simulated');
    expect(provenance([], null)).toMatchObject({ state: 'none', headline: 'NO MATRIX DATA LOADED' });
  });
});

describe('OPERA epochs', () => {
  it('merges DISP-S1 and DSWx epochs in time order, tagged by product', () => {
    const epochs = operaEpochs(layerInfo(['displacement', 'water_class', 'sai']));
    expect(epochs.map((e) => formatDate(e.time))).toEqual(['2023-01-13', '2023-01-25', '2023-02-06', '2023-03-05']);
    expect(epochs[1].products).toEqual(['DISP-S1', 'DSWx']);
    expect(epochs[3].products).toEqual(['DSWx']);
    expect(epochs).toHaveLength(new Set([...DISP_TIMES, ...DSWX_TIMES]).size);
  });

  it('ignores unavailable layers and treats naive timestamps as UTC', () => {
    expect(operaEpochs(layerInfo([]))).toEqual([]);
    expect(normaliseTime('2024-01-05T18:00:00')).toBe('2024-01-05T18:00:00Z');
    expect(normaliseTime('2024-01-05T18:00:00+00:00')).toBe('2024-01-05T18:00:00+00:00');
  });
});

describe('facility footprints', () => {
  it('maps hazard levels to green / yellow / red, and unknown to grey', () => {
    expect(alertColorName('NONE')).toBe('green');
    expect(alertColorName('WATCH_TIER1')).toBe('yellow');
    expect(alertColorName('ALERT_TIER2')).toBe('yellow');
    expect(alertColorName('CRITICAL_TIER3')).toBe('red');
    expect(alertColorName(null)).toBe('unknown');
  });

  it('builds closed bbox polygons carrying the tooltip attributes', () => {
    const fc = facilitiesGeoJson(FACILITIES, { sierrita: SIERRITA_ALERT, mission: null });
    const s = fc.features[0];
    expect(s.geometry.coordinates[0]).toEqual([[-111.12, 31.83], [-111.04, 31.83], [-111.04, 31.9], [-111.12, 31.9], [-111.12, 31.83]]);
    expect(s.properties).toMatchObject({ id: 'sierrita', alert_color: 'red', hazard_level: 'CRITICAL_TIER3', synthetic_inputs: true, mine_type: 'copper-molybdenum' });
    expect(fc.features[1].properties).toMatchObject({ alert_color: 'unknown', hazard_level: null, synthetic_inputs: null });
  });
});

describe('camera', () => {
  it('centres on the bbox, zooms in further on a bigger screen, and tilts for terrain', () => {
    const small = cameraForBbox(SIERRITA_BBOX, 600, 400);
    const big = cameraForBbox(SIERRITA_BBOX, 1600, 1000);
    expect(small.longitude).toBeCloseTo(-111.08, 3);
    expect(small.latitude).toBeCloseTo(31.865, 2);
    expect(big.zoom).toBeGreaterThan(small.zoom);
    expect(small.pitch).toBe(50);
    expect(cameraForBbox(SIERRITA_BBOX, 600, 400, 0).pitch).toBe(0);
  });

  it('uses Mapbox terrain with a token and open Terrarium tiles without', () => {
    const mb = terrainSource('pk.test');
    expect(mb.provider).toBe('mapbox');
    expect(mb.elevationData).toContain('mapbox.terrain-rgb');
    expect(mb.elevationDecoder.offset).toBe(-10000);
    const open = terrainSource(undefined);
    expect(open.provider).toBe('open');
    expect(open.elevationData).toContain('terrarium');
    expect(open.elevationDecoder.offset).toBe(-32768);
  });
});

describe('layer stack', () => {
  const matrices = Object.fromEntries(
    (['sai', 'displacement', 'flow_accumulation', 'anomaly_regime', 'spectral_rmse', 'coherence'] as OverlayId[]).map((id) => [id, MATRICES[id](null)]),
  ) as Record<OverlayId, Matrix>;
  const base: LayerState = {
    facilities: FACILITIES,
    latestAlerts: { sierrita: SIERRITA_ALERT },
    selectedId: 'sierrita',
    activeOverlays: ['anomaly_regime'],
    matrices,
    mask: MATRICES.observability_mask(null),
    showMask: true,
    overlayOpacity: 0.8,
    terrain: false,
    terrainSource: terrainSource(undefined),
    pickedPoint: null,
  };
  const ids = (s: Partial<LayerState>) => buildLayers({ ...base, ...s }).map((l) => l.id);

  it('switches matrix layers with the active overlays, in a fixed draw order', () => {
    expect(ids({})).toEqual(['facilities', 'matrix-anomaly_regime', 'observability-mask', 'facility-markers']);
    expect(ids({ activeOverlays: ['sai', 'displacement'] })).toEqual(['facilities', 'matrix-displacement', 'matrix-sai', 'observability-mask', 'facility-markers']);
    const all = ids({ activeOverlays: [...OVERLAY_ORDER].reverse() }).filter((i) => i.startsWith('matrix-'));
    expect(all).toEqual(OVERLAY_ORDER.map((o) => `matrix-${o}`));
  });

  it('skips overlays whose matrix has not loaded and drops the mask with no overlay', () => {
    expect(ids({ activeOverlays: ['sai'], matrices: {} })).toEqual(['facilities', 'facility-markers']);
    expect(ids({ activeOverlays: [] })).toEqual(['facilities', 'facility-markers']);
    expect(ids({ showMask: false })).toEqual(['facilities', 'matrix-anomaly_regime', 'facility-markers']);
  });

  it('adds the terrain and drapes every layer on it when 3D is on', () => {
    const layers = buildLayers({ ...base, terrain: true, pickedPoint: [-111.1, 31.86] });
    expect(layers.map((l) => l.id)).toEqual([
      'terrain',
      'facilities@draped',
      'matrix-anomaly_regime@draped',
      'observability-mask@draped',
      'facility-markers@draped',
      'picked-point@draped',
    ]);
    for (const l of layers.slice(1)) expect(l.props.extensions).toHaveLength(1);
    expect(layers[0].props).toMatchObject({ operation: 'terrain+draw', elevationData: expect.stringContaining('terrarium') });
  });

  it('places each matrix at its bbox and keeps the selected facility out of the markers', () => {
    const layers = buildLayers(base);
    expect(layers.find((l) => l.id === 'matrix-anomaly_regime')!.props).toMatchObject({ bounds: SIERRITA_BBOX, opacity: 0.8 });
    const markers = layers.find((l) => l.id === 'facility-markers')!.props.data as { properties: { id: string } }[];
    expect(markers.map((m) => m.properties.id)).toEqual(['mission']);
    const fill = (layers[0].props as unknown as { getFillColor: (f: unknown) => number[] }).getFillColor({ properties: { alert_color: 'red', id: 'mission' } });
    expect(fill.slice(0, 3)).toEqual(ALERT_COLORS.red);
  });
});
