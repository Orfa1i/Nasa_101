/**
 * Dashboard state: facilities and alerts, the selected facility, active overlays, the OPERA epoch,
 * the loaded matrices (including the RMSE / coherence quality layers and the observability mask),
 * and the picked pixel. All data comes from the FastAPI backend.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, ApiError, type Alert, type DashboardSummary, type Facility, type LayerInfo, type Matrix, type PixelResponse } from '@/utils/api';
import { reportIssue } from '@/utils/issues';
import { OVERLAY_BY_ID, type OverlayId } from '@/utils/overlays';
import { operaEpochs, type Epoch } from '@/utils/provenance';

/** `stale`: the backend is unreachable and a cached copy (localStorage) is shown. */
export type LoadState = 'idle' | 'loading' | 'ready' | 'stale' | 'error';

export interface MatrixSlot {
  state: LoadState;
  matrix: Matrix | null;
  error: string | null;
}

export interface PixelPick {
  lon: number;
  lat: number;
  state: LoadState;
  data: PixelResponse | null;
  error: string | null;
}

export const QUALITY_LAYERS: OverlayId[] = ['spectral_rmse', 'coherence'];
const MASK_LAYER = 'observability_mask';

function message(err: unknown): string {
  if (err instanceof ApiError) return err.status === 404 ? `not available: ${err.detail}` : err.message;
  return err instanceof Error ? err.message : String(err);
}

function isAbort(err: unknown): boolean {
  return err instanceof Error && err.name === 'AbortError';
}

export interface StressOverlay {
  matrix: Matrix;
  footprint: [number, number][];
  intensity: number;
}

const REGISTRY_CACHE_KEY = 'lithocreep.registry.v1';

interface RegistryCache {
  at: number;
  facilities: Facility[];
  latestAlerts: Record<string, Alert | null>;
}

/** localStorage can be missing, full or blocked (private windows): every access is guarded. */
export function saveRegistryCache(c: RegistryCache): void {
  try {
    window.localStorage.setItem(REGISTRY_CACHE_KEY, JSON.stringify(c));
  } catch {
    /* storage unavailable: the cache is a convenience only */
  }
}

export function loadRegistryCache(): RegistryCache | null {
  try {
    const raw = window.localStorage.getItem(REGISTRY_CACHE_KEY);
    if (!raw) return null;
    const c = JSON.parse(raw) as RegistryCache;
    return Array.isArray(c.facilities) && c.facilities.length ? c : null;
  } catch {
    return null;
  }
}

function emptyPixel(facilityId: string, lat: number, lon: number, unavailable: Record<string, string>): PixelResponse {
  return { facility_id: facilityId, lat, lon, utm_x: NaN, utm_y: NaN, utm_crs: '', kinematics: null, spectral: null, hydrology: null, classification: null, unavailable };
}

export function useDashboard(initialOverlays: OverlayId[] = ['anomaly_regime']) {
  const [facilities, setFacilities] = useState<Facility[]>([]);
  const [latestAlerts, setLatestAlerts] = useState<Record<string, Alert | null>>({});
  const [registryState, setRegistryState] = useState<LoadState>('loading');
  const [registryError, setRegistryError] = useState<string | null>(null);
  const [registryCachedAt, setRegistryCachedAt] = useState<number | null>(null);
  const [stressOverlay, setStressOverlay] = useState<StressOverlay | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [layerInfo, setLayerInfo] = useState<LayerInfo[]>([]);
  const [layerInfoState, setLayerInfoState] = useState<LoadState>('idle');
  const [activeOverlays, setActiveOverlays] = useState<OverlayId[]>(initialOverlays);
  const [epoch, setEpoch] = useState<string | null>(null); // null = latest
  const [matrices, setMatrices] = useState<Partial<Record<OverlayId, MatrixSlot>>>({});
  const [mask, setMask] = useState<MatrixSlot>({ state: 'idle', matrix: null, error: null });
  const [showMask, setShowMask] = useState(true);
  const [terrain, setTerrain] = useState(true);
  const [overlayOpacity, setOverlayOpacity] = useState(0.85);
  const [pick, setPick] = useState<PixelPick | null>(null);
  const [summary, setSummary] = useState<{ state: LoadState; data: DashboardSummary | null; error: string | null }>({
    state: 'idle',
    data: null,
    error: null,
  });

  // ---- registry: facilities + latest alert per facility ------------------------------------
  const loadRegistry = useCallback(async (signal?: AbortSignal) => {
    setRegistryState('loading');
    setRegistryError(null);
    try {
      const list = await api.facilities(signal);
      setFacilities(list);
      const entries = await Promise.all(
        list.map(async (f) => {
          try {
            const alerts = await api.alerts(f.id, signal);
            const latest = [...alerts].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0] ?? null;
            return [f.id, latest] as const;
          } catch (err) {
            if (isAbort(err)) throw err;
            return [f.id, null] as const;
          }
        }),
      );
      const alerts = Object.fromEntries(entries);
      setLatestAlerts(alerts);
      setRegistryState('ready');
      setRegistryCachedAt(null);
      saveRegistryCache({ at: Date.now(), facilities: list, latestAlerts: alerts });
    } catch (err) {
      if (isAbort(err)) return;
      setRegistryError(message(err));
      const cached = loadRegistryCache();
      if (cached) {
        // API outage: keep working from the last good registry, clearly marked as an offline copy.
        setFacilities(cached.facilities);
        setLatestAlerts(cached.latestAlerts);
        setRegistryCachedAt(cached.at);
        setRegistryState('stale');
        reportIssue({ source: 'cache', message: 'Facility registry unavailable', detail: message(err), fallback: `cached copy from ${new Date(cached.at).toISOString()}` });
      } else {
        setRegistryState('error');
      }
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void loadRegistry(ctrl.signal);
    return () => ctrl.abort();
  }, [loadRegistry]);

  // ---- per-facility layer listing ------------------------------------------------------------
  useEffect(() => {
    setLayerInfo([]);
    setStressOverlay(null);
    setMatrices({});
    setMask({ state: 'idle', matrix: null, error: null });
    setPick(null);
    setEpoch(null);
    if (!selectedId) {
      setLayerInfoState('idle');
      return;
    }
    const ctrl = new AbortController();
    setLayerInfoState('loading');
    api
      .layers(selectedId, ctrl.signal)
      .then((info) => {
        setLayerInfo(info);
        setLayerInfoState('ready');
      })
      .catch((err) => {
        if (!isAbort(err)) setLayerInfoState('error');
      });
    return () => ctrl.abort();
  }, [selectedId]);

  // Facility-level inspection summary (setback history, GISTM screening) for the inspector.
  useEffect(() => {
    if (!selectedId) {
      setSummary({ state: 'idle', data: null, error: null });
      return;
    }
    const ctrl = new AbortController();
    setSummary({ state: 'loading', data: null, error: null });
    api
      .dashboard(selectedId, ctrl.signal)
      .then((data) => setSummary({ state: 'ready', data, error: null }))
      .catch((err) => {
        if (!isAbort(err)) setSummary({ state: 'error', data: null, error: message(err) });
      });
    return () => ctrl.abort();
  }, [selectedId]);

  const available = useMemo(() => new Set(layerInfo.filter((l) => l.available).map((l) => l.layer)), [layerInfo]);
  const epochs: Epoch[] = useMemo(() => operaEpochs(layerInfo), [layerInfo]);

  // ---- matrices: active overlays + quality layers + mask --------------------------------------
  const wanted = useMemo(() => {
    const ids = new Set<OverlayId>([...activeOverlays, ...QUALITY_LAYERS]);
    return [...ids];
  }, [activeOverlays]);

  const requested = useRef(new Map<string, string>()); // overlay -> request key in flight / loaded

  useEffect(() => {
    requested.current.clear();
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId || layerInfoState !== 'ready') return;
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      for (const id of wanted) {
        const def = OVERLAY_BY_ID[id];
        const info = layerInfo.find((l) => l.layer === def.layer);
        const t = info?.time_dependent ? epoch : null;
        const key = `${selectedId}|${def.layer}|${t ?? 'latest'}`;
        if (requested.current.get(id) === key) continue;
        if (!available.has(def.layer)) {
          requested.current.set(id, key);
          setMatrices((m) => ({ ...m, [id]: { state: 'error', matrix: null, error: info?.detail ?? 'layer not available for this facility' } }));
          continue;
        }
        requested.current.set(id, key);
        setMatrices((m) => ({ ...m, [id]: { state: 'loading', matrix: m[id]?.matrix ?? null, error: null } }));
        api
          .matrix(selectedId, def.layer, t, ctrl.signal)
          .then((matrix) => setMatrices((m) => ({ ...m, [id]: { state: 'ready', matrix, error: null } })))
          .catch((err) => {
            if (isAbort(err)) {
              requested.current.delete(id);
              return;
            }
            setMatrices((m) => ({ ...m, [id]: { state: 'error', matrix: null, error: message(err) } }));
          });
      }
    }, 120); // debounce timeline scrubbing
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [selectedId, layerInfoState, layerInfo, available, wanted, epoch]);

  useEffect(() => {
    if (!selectedId || layerInfoState !== 'ready') return;
    if (!available.has(MASK_LAYER)) {
      setMask({ state: 'error', matrix: null, error: 'no observability mask for this facility' });
      return;
    }
    const ctrl = new AbortController();
    setMask({ state: 'loading', matrix: null, error: null });
    api
      .matrix(selectedId, MASK_LAYER, null, ctrl.signal)
      .then((matrix) => setMask({ state: 'ready', matrix, error: null }))
      .catch((err) => {
        if (!isAbort(err)) setMask({ state: 'error', matrix: null, error: message(err) });
      });
    return () => ctrl.abort();
  }, [selectedId, layerInfoState, available]);

  // ---- pixel picking --------------------------------------------------------------------------
  const pickAbort = useRef<AbortController | null>(null);
  const pickPixel = useCallback(
    (lon: number, lat: number) => {
      if (!selectedId) return;
      pickAbort.current?.abort();
      const ctrl = new AbortController();
      pickAbort.current = ctrl;
      setPick({ lon, lat, state: 'loading', data: null, error: null });
      api
        .pixel(selectedId, lat, lon, ctrl.signal)
        .then((data) => setPick({ lon, lat, state: 'ready', data, error: null }))
        .catch((err) => {
          if (isAbort(err)) return;
          // 404 "no data at this point" lists why each product is missing: show that per panel.
          const body = err instanceof ApiError ? (err.body as { unavailable?: Record<string, string> } | undefined) : undefined;
          if (err instanceof ApiError && err.status === 404 && body?.unavailable) {
            setPick({ lon, lat, state: 'ready', data: emptyPixel(selectedId, lat, lon, body.unavailable), error: null });
            return;
          }
          setPick({ lon, lat, state: 'error', data: null, error: message(err) });
        });
    },
    [selectedId],
  );

  const toggleOverlay = useCallback((id: OverlayId) => {
    setActiveOverlays((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  }, []);

  const selectFacility = useCallback((id: string | null) => setSelectedId(id), []);

  const selected = facilities.find((f) => f.id === selectedId) ?? null;

  return {
    facilities,
    latestAlerts,
    registryState,
    registryError,
    registryCachedAt,
    stressOverlay,
    setStressOverlay,
    reloadRegistry: () => loadRegistry(),
    selected,
    selectedId,
    selectFacility,
    layerInfo,
    layerInfoState,
    available,
    activeOverlays,
    toggleOverlay,
    setActiveOverlays,
    epochs,
    epoch,
    setEpoch,
    matrices,
    mask,
    showMask,
    setShowMask,
    terrain,
    setTerrain,
    overlayOpacity,
    setOverlayOpacity,
    pick,
    pickPixel,
    summary,
    clearPick: () => setPick(null),
  };
}

export type Dashboard = ReturnType<typeof useDashboard>;
