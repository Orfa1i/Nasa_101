/**
 * 3D map: deck.gl over a Mapbox GL / MapLibre GL basemap.
 *
 * * TerrainLayer: Mapbox terrain-RGB (with a token) or AWS Terrarium tiles, textured, 3D.
 * * GeoJsonLayer: facility footprints from /api/facilities, coloured by the latest alert.
 * * BitmapLayer per active matrix from /api/facilities/{id}/matrices, draped on the terrain,
 *   with unobservable (vegetated / decorrelated) cells transparent and hatched.
 * * Selecting a facility flies the camera to its bbox; clicking a cell loads /pixel.
 */
import { FlyToInterpolator, type MapViewState, type PickingInfo } from '@deck.gl/core';
import DeckGL from '@deck.gl/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import Basemap, { basemapProvider } from '@/components/Basemap';
import { Legend, QualityPanel, SimulatedBackdrop, Watermark, type HoverReadout } from '@/components/MapHud';
import type { Dashboard } from '@/hooks/useDashboard';
import type { DataSource, Matrix } from '@/utils/api';
import {
  ALERT_LABELS,
  WORLD_VIEW,
  buildLayers,
  cameraForBbox,
  facilityBbox,
  terrainSources,
  type FacilityProps,
} from '@/utils/mapLayers';
import { OVERLAY_BY_ID, valueAt, type OverlayId } from '@/utils/overlays';
import { reportIssue } from '@/utils/issues';
import { provenance } from '@/utils/provenance';

type MapViewerProps = { dash: Dashboard; mapboxToken?: string };

function useElementSize(ref: React.RefObject<HTMLElement>) {
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setSize({ width: el.clientWidth, height: el.clientHeight });
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

const escapeHtml = (s: string) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);

function inBbox(lon: number, lat: number, b: [number, number, number, number]): boolean {
  return lon >= b[0] && lon <= b[2] && lat >= b[1] && lat <= b[3];
}

/** lon/lat of a pick: exact cell from a bitmap pick (correct on 3D terrain), else the unprojected coordinate. */
export function pickedLonLat(info: Pick<PickingInfo, 'coordinate' | 'layer'> & { bitmap?: { uv: number[] } | null }): [number, number] | null {
  const bounds = (info.layer?.props as { bounds?: number[] } | undefined)?.bounds;
  if (info.bitmap && bounds && bounds.length === 4) {
    const [x0, y0, x1, y1] = bounds as [number, number, number, number];
    return [x0 + info.bitmap.uv[0] * (x1 - x0), y1 - info.bitmap.uv[1] * (y1 - y0)];
  }
  return info.coordinate ? [info.coordinate[0], info.coordinate[1]] : null;
}

export default function MapViewer({ dash, mapboxToken }: MapViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const size = useElementSize(containerRef);
  const [viewState, setViewState] = useState<MapViewState>({ ...WORLD_VIEW });
  const [hover, setHover] = useState<HoverReadout | null>(null);
  const [basemapError, setBasemapError] = useState<string | null>(null);
  const [deckError, setDeckError] = useState<string | null>(null);
  const [basemapName, setBasemapName] = useState(basemapProvider(mapboxToken));
  const sources = useMemo(() => terrainSources(mapboxToken), [mapboxToken]);
  const [sourceIdx, setSourceIdx] = useState(0);
  const terrain = sources[Math.min(sourceIdx, sources.length - 1)];
  const sized = size.width > 0 && size.height > 0;

  // Draped layers only render where terrain exists. If a provider's tiles cannot be loaded
  // (offline, Mapbox outage, bad token) try the next provider, then fall back to flat rendering.
  const [terrainStatus, setTerrainStatus] = useState<'loading' | 'ok' | 'failed'>('loading');
  const terrainErrors = useRef(0);
  useEffect(() => {
    if (dash.terrain) {
      terrainErrors.current = 0;
      setSourceIdx(0);
      setTerrainStatus('loading');
    }
  }, [dash.terrain]);
  const onTerrainTile = useCallback(
    (ok: boolean) => {
      if (ok) {
        setTerrainStatus((st) => (st === 'ok' ? st : 'ok'));
        return;
      }
      terrainErrors.current += 1;
      if (terrainErrors.current < 3 || terrainStatus !== 'loading') return;
      terrainErrors.current = 0;
      if (sourceIdx < sources.length - 1) {
        reportIssue({ source: 'terrain', message: `${terrain.provider === 'mapbox' ? 'Mapbox' : 'Open'} terrain tiles unavailable`, fallback: 'open Terrarium terrain tiles' });
        setSourceIdx((i) => i + 1);
      } else {
        reportIssue({ source: 'terrain', message: 'Terrain tiles unavailable', fallback: 'flat overlays (no 3D terrain)' });
        setTerrainStatus('failed');
      }
    },
    [sourceIdx, sources.length, terrain.provider, terrainStatus],
  );
  const terrainOn = dash.terrain && terrainStatus !== 'failed';

  // Fly to the active facility's bbox (or back to the world view).
  useEffect(() => {
    if (!sized) return;
    const target = dash.selected
      ? cameraForBbox(facilityBbox(dash.selected), size.width, size.height, dash.terrain ? 50 : 0)
      : { ...WORLD_VIEW };
    const reduced = prefersReducedMotion();
    setViewState((v) => ({
      ...v,
      ...target,
      transitionDuration: reduced ? 0 : 1800,
      transitionInterpolator: reduced ? undefined : new FlyToInterpolator({ speed: 1.6 }),
    }));
    // Re-frame only when the selection (or the first measured size) changes, not on every resize.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dash.selectedId, sized]);

  const readyMatrices = useMemo(() => {
    const out: Partial<Record<OverlayId, Matrix>> = {};
    for (const [id, slot] of Object.entries(dash.matrices)) if (slot?.matrix) out[id as OverlayId] = slot.matrix;
    return out;
  }, [dash.matrices]);

  const layers = useMemo(
    () =>
      buildLayers({
        facilities: dash.facilities,
        latestAlerts: dash.latestAlerts,
        selectedId: dash.selectedId,
        activeOverlays: dash.selectedId ? dash.activeOverlays : [],
        matrices: readyMatrices,
        mask: dash.mask.matrix,
        showMask: dash.showMask,
        overlayOpacity: dash.overlayOpacity,
        terrain: terrainOn,
        terrainSource: terrain,
        pickedPoint: dash.pick ? [dash.pick.lon, dash.pick.lat] : null,
        onTerrainTile,
        stressOverlay: dash.stressOverlay,
      }),
    [dash.facilities, dash.latestAlerts, dash.selectedId, dash.activeOverlays, readyMatrices, dash.mask.matrix, dash.showMask, dash.overlayOpacity, terrainOn, terrain, dash.pick, onTerrainTile, dash.stressOverlay],
  );

  const shownSources: DataSource[] = useMemo(() => {
    const shown = dash.selectedId ? dash.activeOverlays.map((id) => readyMatrices[id]?.source).filter((s): s is DataSource => !!s) : [];
    return dash.stressOverlay ? [...shown, dash.stressOverlay.matrix.source] : shown;
  }, [dash.selectedId, dash.activeOverlays, readyMatrices, dash.stressOverlay]);
  const prov = provenance(shownSources, dash.selectedId ? dash.latestAlerts[dash.selectedId] : null);
  const shownEpoch = dash.activeOverlays.map((id) => readyMatrices[id]?.time).find((t): t is string => !!t) ?? null;

  const onClick = useCallback(
    (info: PickingInfo) => {
      if (info.layer?.id?.startsWith('facilit') && info.object) {
        const id = (info.object as GeoJSON.Feature<GeoJSON.Polygon, FacilityProps>).properties.id;
        if (id !== dash.selectedId) {
          dash.selectFacility(id);
          return;
        }
      }
      if (!dash.selected) return;
      const ll = pickedLonLat(info as PickingInfo & { bitmap?: { uv: number[] } });
      if (ll && inBbox(ll[0], ll[1], facilityBbox(dash.selected))) dash.pickPixel(ll[0], ll[1]);
    },
    [dash],
  );

  const onHover = useCallback(
    (info: PickingInfo) => {
      const ll = dash.selected ? pickedLonLat(info as PickingInfo & { bitmap?: { uv: number[] } }) : null;
      if (!ll || !dash.selected || !inBbox(ll[0], ll[1], facilityBbox(dash.selected))) {
        setHover((h) => (h ? null : h));
        return;
      }
      const rmse = dash.matrices.spectral_rmse?.matrix;
      const coh = dash.matrices.coherence?.matrix;
      setHover({
        lon: ll[0],
        lat: ll[1],
        rmse: rmse ? valueAt(rmse, ll[0], ll[1]) : null,
        coherence: coh ? valueAt(coh, ll[0], ll[1]) : null,
        maskCode: dash.mask.matrix ? valueAt(dash.mask.matrix, ll[0], ll[1]) : null,
      });
    },
    [dash.selected, dash.matrices, dash.mask.matrix],
  );

  const getTooltip = useCallback(
    (info: PickingInfo) => {
      const facility = info.layer?.id?.startsWith('facilit') && info.object ? (info.object as GeoJSON.Feature<GeoJSON.Polygon, FacilityProps>) : null;
      if (facility && facility.properties.id !== dash.selectedId) {
        const p = facility.properties;
        const rows: [string, string][] = [
          ['Country', p.country],
          ['Commodity', p.mine_type],
          ['Embankment', p.embankment_type],
          ['Status', p.status],
          ['Alert', p.hazard_level ? `${p.hazard_level} (${ALERT_LABELS[p.alert_color]})` : ALERT_LABELS.unknown],
        ];
        if (p.synthetic_inputs) rows.push(['Inputs', 'contain SYNTHETIC/MOCK data']);
        return {
          html: `<div data-testid="facility-tooltip"><strong>${escapeHtml(p.name)}</strong><table>${rows
            .map(([k, v]) => `<tr><td style="opacity:.7;padding-right:8px">${escapeHtml(k)}</td><td>${escapeHtml(v)}</td></tr>`)
            .join('')}</table></div>`,
          style: { background: 'rgba(2,6,23,.92)', color: '#e2e8f0', fontSize: '12px', borderRadius: '6px', maxWidth: '280px' },
        };
      }
      // Inside the selected facility: report the cell values of the active matrices.
      if (info.layer?.id?.startsWith('matrix-') || facility) {
        const ll = pickedLonLat(info as PickingInfo & { bitmap?: { uv: number[] } });
        if (!ll) return null;
        const lines = dash.activeOverlays
          .map((id) => {
            const m = readyMatrices[id];
            if (!m) return null;
            const v = valueAt(m, ll[0], ll[1]);
            const def = OVERLAY_BY_ID[id];
            return `${escapeHtml(def.label)}: ${v === null ? 'no data' : v.toFixed(def.decimals)} ${escapeHtml(m.units)}`;
          })
          .filter(Boolean);
        return {
          html: `<div data-testid="matrix-tooltip">${lines.join('<br/>')}<br/><span style="opacity:.7">${ll[1].toFixed(5)}, ${ll[0].toFixed(5)}</span></div>`,
          style: { background: 'rgba(2,6,23,.92)', color: '#e2e8f0', fontSize: '12px', borderRadius: '6px' },
        };
      }
      return null;
    },
    [dash.activeOverlays, dash.selectedId, readyMatrices],
  );

  const layerIds = layers.map((l) => l.id).join(',');

  return (
    <div
      ref={containerRef}
      data-testid="map-viewer"
      data-layers={layerIds}
      data-selected={dash.selectedId ?? ''}
      data-longitude={viewState.longitude.toFixed(5)}
      data-latitude={viewState.latitude.toFixed(5)}
      data-zoom={viewState.zoom.toFixed(2)}
      data-pitch={(viewState.pitch ?? 0).toFixed(1)}
      data-terrain={dash.terrain ? terrainStatus : 'off'}
      className="relative h-full w-full overflow-hidden bg-slate-900"
    >
      <DeckGL
        viewState={viewState}
        onViewStateChange={({ viewState: vs }) => setViewState(vs as MapViewState)}
        controller={{ dragRotate: true, touchRotate: true }}
        layers={layers}
        getTooltip={getTooltip}
        onClick={onClick}
        onHover={onHover}
        onError={(err) => setDeckError(err.message)}
        getCursor={({ isHovering, isDragging }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'crosshair')}
      >
        <Basemap mapboxToken={mapboxToken} onError={setBasemapError} onProvider={setBasemapName} />
      </DeckGL>

      <SimulatedBackdrop prov={prov} />

      <div className="pointer-events-none absolute inset-x-2 top-2 flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div className="flex w-full min-w-0 flex-col items-start gap-2 md:w-auto md:max-w-md">
          <Watermark prov={prov} epoch={shownEpoch} />
          {dash.stressOverlay && (
            <p
              role="status"
              data-testid="stress-overlay-badge"
              className="pointer-events-auto rounded border-2 border-dashed border-amber-400 bg-amber-950/90 px-3 py-1 text-[11px] font-bold tracking-wider text-amber-100"
            >
              TEST OVERLAY · SIMULATED STRESS TEST ({Math.round(dash.stressOverlay.intensity * 100)} %)
            </p>
          )}
        </div>
        {dash.selectedId && (
          <QualityPanel rmse={dash.matrices.spectral_rmse} coherence={dash.matrices.coherence} mask={dash.mask.matrix} hover={hover} />
        )}
      </div>

      <div className="pointer-events-none absolute bottom-7 left-2 right-32 flex max-h-[45%] flex-col items-start gap-2 lg:right-2">
        {dash.selectedId && (
          <Legend active={dash.activeOverlays} matrices={dash.matrices} showMask={dash.showMask} hasMask={!!dash.mask.matrix} />
        )}
        {dash.terrain && terrainStatus === 'failed' && (
          <p role="status" data-testid="terrain-fallback" className="pointer-events-auto rounded bg-amber-950/90 px-2 py-1 text-[11px] text-amber-100">
            3D terrain tiles unavailable: showing overlays flat.
          </p>
        )}
        {(basemapError || deckError) && (
          <p
            role="alert"
            data-testid="map-error"
            title={deckError ?? basemapError ?? ''}
            className="pointer-events-auto max-w-full truncate rounded bg-red-950/90 px-2 py-1 text-[11px] text-red-200"
          >
            {deckError ? `Map rendering error: ${deckError}` : 'Basemap unavailable: overlays still shown.'}
          </p>
        )}
      </div>

      <p
        data-testid="attribution"
        className="absolute bottom-0 right-0 max-w-full truncate rounded-tl bg-slate-950/70 px-2 py-0.5 text-[10px] text-slate-400"
      >
        {basemapName} · deck.gl{terrainOn ? ` · ${terrain.attribution}` : ''}
      </p>
    </div>
  );
}
