/**
 * Scenario panel:
 *
 * * Brumadinho historical backtest: the Phase 3 inverse-velocity analysis of the published
 *   pre-collapse point series (DATA_DIR/brumadinho_historic.csv), issued before the 2019-01-25
 *   collapse. OPERA DISP-S1 does not cover South America, so this is the only kinematic source.
 * * Geotechnical stress-test simulator (Arizona facilities): a hazard slider injects SIMULATED
 *   pore-pressure seepage anomalies (SAI increase, tertiary creep, shrinking pond setback) into the
 *   cached products and re-runs the real Phase 3 / Phase 5 code. Results are test overlays.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Area, CartesianGrid, ComposedChart, Legend, Line, LineChart, ReferenceArea, ReferenceLine, Scatter, Tooltip, XAxis, YAxis } from 'recharts';

import { AXIS, ChartFrame, Empty, LEGEND, NO_ANIMATION, StatTile, StatusBadge, TOOLTIP, fmtDay, fmtMonth, num } from '@/components/ChartKit';
import type { Dashboard } from '@/hooks/useDashboard';
import { api, ApiError, type BacktestResponse, type StressTestResponse } from '@/utils/api';
import { STATUS, VIZ, kinematicRows, ms } from '@/utils/inspector';
import { REGIME_CLASSES } from '@/utils/overlays';
import { inArizona } from '@/utils/scenarios';

function useDebounced<T>(value: T, delay: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return v;
}

const errorText = (err: unknown) => (err instanceof ApiError ? (err.status === 0 ? err.detail : `${err.status}: ${err.detail}`) : err instanceof Error ? err.message : String(err));

// --------------------------------------------------------------------------- //
// Brumadinho backtest
// --------------------------------------------------------------------------- //

interface Missing {
  message: string;
  expected_file?: string;
  expected_columns?: string[];
}

export function BacktestPanel({ dash }: { dash: Dashboard }) {
  const [point, setPoint] = useState<string | null>(null);
  const [cutoff, setCutoff] = useState(0);
  const debouncedCutoff = useDebounced(cutoff, 300);
  const [state, setState] = useState<'loading' | 'ready' | 'missing' | 'error'>('loading');
  const [data, setData] = useState<BacktestResponse | null>(null);
  const [missing, setMissing] = useState<Missing | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setState((s) => (s === 'ready' ? 'ready' : 'loading'));
    api
      .backtest({ point, cutoffDays: debouncedCutoff }, ctrl.signal)
      .then((d) => {
        setData(d);
        setState('ready');
      })
      .catch((err) => {
        if (err instanceof Error && err.name === 'AbortError') return;
        if (err instanceof ApiError && err.status === 404 && typeof err.body === 'object' && err.body && 'expected_columns' in err.body) {
          setMissing(err.body as Missing);
          setState('missing');
          return;
        }
        setError(errorText(err));
        setState('error');
      });
    return () => ctrl.abort();
  }, [point, debouncedCutoff]);

  const header = (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        {data && state === 'ready' ? (
          data.historical ? (
            <span data-testid="backtest-badge" data-kind="historical" className="rounded border-2 border-violet-400 bg-violet-950 px-2 py-0.5 text-[11px] font-bold tracking-widest text-violet-100">
              HISTORICAL DATASET · 2019
            </span>
          ) : (
            <span data-testid="backtest-badge" data-kind="synthetic" className="rounded border-2 border-amber-400 bg-amber-950 px-2 py-0.5 text-[11px] font-bold tracking-widest text-amber-100">
              SYNTHETIC STAND-IN · NOT THE PUBLISHED SERIES
            </span>
          )
        ) : (
          <span className="rounded border border-violet-400/60 px-2 py-0.5 text-[11px] font-bold tracking-widest text-violet-200">HISTORICAL BACKTEST</span>
        )}
        <button type="button" className="text-[11px] text-sky-300 underline" onClick={() => dash.selectFacility('brumadinho')}>
          Show on map
        </button>
      </div>
      <p className="text-sm font-semibold text-slate-100">Brumadinho Dam I, Minas Gerais, Brazil: collapsed 2019-01-25</p>
      <p className="text-[11px] leading-snug text-slate-400">
        Pre-unwrapped OPERA DISP-S1 does not cover South America, so this backtest runs the Phase 3 GP and inverse-velocity analysis on the published
        pre-collapse InSAR point series in <code className="text-slate-300">data/brumadinho_historic.csv</code>.
      </p>
    </div>
  );

  if (state === 'loading' && !data) return <div className="space-y-2">{header}<Empty>Loading the backtest…</Empty></div>;
  if (state === 'missing' && missing)
    return (
      <div className="space-y-2" data-testid="backtest-missing">
        {header}
        <div className="rounded-md border border-dashed border-violet-400/50 p-3 text-[11px] text-slate-300">
          <p className="font-semibold text-slate-100">Historical dataset not supplied</p>
          <p className="mt-1">{missing.message}</p>
          <p className="mt-2 text-slate-400">Place the file at {missing.expected_file} with columns:</p>
          <code className="mt-1 block break-words text-slate-200">{(missing.expected_columns ?? []).join(', ')}</code>
          <p className="mt-2 text-slate-400">Nothing is shown until it is supplied: LithoCreep does not reconstruct or invent the published series.</p>
        </div>
      </div>
    );
  if (state === 'error' || !data)
    return (
      <div className="space-y-2">
        {header}
        <p role="alert" className="rounded border border-red-500/40 bg-red-950/40 p-2 text-[11px] text-red-200">
          Backtest unavailable: {error}
        </p>
      </div>
    );

  const k = data.series;
  const collapse = ms(data.collapse_date);
  const winStart = ms(data.window_start);
  const fit = k.fukuzono ?? null;
  const o = data.outcome;
  const lambdaRows = new Map<number, { t: number; lambda: number | null; fit: number | null; band: [number, number] | null }>();
  const row = (t: number) => {
    let r = lambdaRows.get(t);
    if (!r) lambdaRows.set(t, (r = { t, lambda: null, fit: null, band: null }));
    return r;
  };
  k.times.forEach((t, i) => {
    const v = k.inverse_velocity_yr_mm?.[i];
    const tt = ms(t);
    if (typeof v === 'number' && Number.isFinite(v) && tt >= winStart) row(tt).lambda = v;
  });
  fit?.line_times.forEach((t, i) => {
    const tt = ms(t);
    if (tt >= winStart) {
      const r = row(tt);
      r.fit = fit.line_yr_mm[i];
      r.band = [fit.line_lower_yr_mm[i], fit.line_upper_yr_mm[i]];
    }
  });
  const rows = [...lambdaRows.values()].sort((a, b) => a.t - b.t);
  const xMax = Math.max(collapse, o.upper ? ms(o.upper) : collapse) + 3 * 86400e3;
  const kin = kinematicRows(k);
  const late = (o.error_days ?? 0) >= 0;

  return (
    <div className="space-y-2" data-testid="backtest">
      {header}
      <p className="text-[11px] text-slate-400" data-testid="backtest-source">
        Source: {data.citation}
      </p>
      <div className="flex items-end gap-2">
        <label className="min-w-0 flex-1 text-[11px] text-slate-400">
          Monitoring point
          <select
            value={data.point_id}
            onChange={(e) => setPoint(e.target.value)}
            data-testid="backtest-point"
            className="mt-0.5 w-full rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-xs text-slate-100"
          >
            {data.points.map((p) => (
              <option key={p.point_id} value={p.point_id}>
                {p.point_id}
                {p.zone ? ` (${p.zone})` : ''}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="block text-[11px] text-slate-400">
        Issue the forecast <span className="font-mono text-slate-200" data-testid="backtest-cutoff-label">{cutoff}</span> days before the collapse
        <input
          type="range"
          min={0}
          max={60}
          step={1}
          value={cutoff}
          onChange={(e) => setCutoff(Number(e.target.value))}
          className="w-full accent-violet-400"
          aria-label="Days before the collapse"
          data-testid="backtest-cutoff"
        />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <StatTile testId="backtest-issued" label="Forecast issued (last epoch used)" value={<span className="text-sm">{fmtDay(ms(o.issued))}</span>} sub={<span className="text-[11px] text-slate-400">{num(o.lead_days, 0)} days before collapse</span>} />
        <StatTile
          testId="backtest-estimate"
          label="Estimated change in velocity"
          value={<span className="text-sm">{o.estimate ? fmtDay(ms(o.estimate)) : '—'}</span>}
          sub={<span className="text-[11px] text-slate-400">{o.lower && o.upper ? `95 %: ${fmtDay(ms(o.lower))} – ${fmtDay(ms(o.upper))}` : (o.status ?? 'no trend')}</span>}
        />
        <StatTile
          testId="backtest-error"
          label="Estimate vs actual collapse"
          value={<span className="text-sm">{o.error_days === null ? '—' : `${o.error_days >= 0 ? '+' : ''}${o.error_days.toFixed(1)} d`}</span>}
          sub={<span className="text-[11px] text-slate-400">{o.error_days === null ? 'no estimate' : late ? 'after the collapse (late)' : 'before the collapse (early)'}</span>}
        />
        <StatTile
          testId="backtest-within"
          label="Collapse inside the 95 % window"
          value={<span className="text-sm">{o.collapse_within_95 === null ? '—' : o.collapse_within_95 ? 'Yes' : 'No'}</span>}
          sub={<StatusBadge level={o.collapse_within_95 === null ? 'unknown' : o.collapse_within_95 ? 'good' : 'critical'} label={o.collapse_within_95 ? 'Captured' : o.collapse_within_95 === null ? 'n/a' : 'Missed'} />}
        />
      </div>

      <ChartFrame
        title={`${data.window_days}-day pre-collapse inverse velocity Λ(t) at ${data.point_id}`}
        testId="chart-backtest-lambda"
        height={190}
        caption={
          fit ? (
            <>
              <span className="font-semibold text-slate-200">{fit.label}.</span> The red line is the actual collapse (2019-01-25); the shaded band is the 95 % window of
              the trend change estimated from data up to {fmtDay(ms(o.issued))}.
            </>
          ) : (
            <>No converging inverse-velocity trend at this issue date ({o.status ?? 'unknown status'}).</>
          )
        }
      >
        {(width) => (
          <ComposedChart width={width} height={190} data={rows} margin={{ top: 14, right: 10, bottom: 0, left: -6 }}>
            <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
            {o.lower && o.upper && <ReferenceArea x1={ms(o.lower)} x2={ms(o.upper)} fill={VIZ.series2} fillOpacity={0.15} strokeOpacity={0} />}
            <XAxis dataKey="t" type="number" scale="time" domain={[winStart, xMax]} tickFormatter={fmtDay} minTickGap={28} {...AXIS} />
            <YAxis width={58} tickFormatter={(v: number) => v.toFixed(4)} {...AXIS} />
            <ReferenceLine y={0} stroke={VIZ.muted} />
            <ReferenceLine x={collapse} stroke={STATUS.critical.color} strokeWidth={2} label={{ value: 'Collapse', fill: VIZ.text, fontSize: 9, position: 'insideTopLeft' }} />
            <Tooltip {...TOOLTIP} labelFormatter={(v) => fmtDay(Number(v))} formatter={(v: number | [number, number], name) => [Array.isArray(v) ? `${v[0].toFixed(4)} – ${v[1].toFixed(4)}` : v.toFixed(4), name]} />
            <Legend {...LEGEND} />
            <Area name="95 % prediction band" dataKey="band" fill={VIZ.series2} fillOpacity={0.2} stroke="none" connectNulls {...NO_ANIMATION} />
            <Scatter name="Observed Λ = 1/|v|" data={rows.filter((r) => r.lambda !== null)} dataKey="lambda" fill={VIZ.series1} shape="circle" legendType="circle" {...NO_ANIMATION} />
            <Line name="Inverse-velocity fit" dataKey="fit" stroke={VIZ.series2} strokeWidth={2} strokeDasharray="5 3" dot={false} connectNulls {...NO_ANIMATION} />
          </ComposedChart>
        )}
      </ChartFrame>

      <ChartFrame title={`Cumulative LOS displacement at ${data.point_id} (mm)`} testId="chart-backtest-displacement" height={150}>
        {(width) => (
          <ComposedChart width={width} height={150} data={kin} margin={{ top: 4, right: 10, bottom: 0, left: -12 }}>
            <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', collapse]} tickFormatter={fmtMonth} minTickGap={24} {...AXIS} />
            <YAxis width={48} {...AXIS} />
            <ReferenceLine x={collapse} stroke={STATUS.critical.color} strokeWidth={2} />
            <Tooltip {...TOOLTIP} labelFormatter={(v) => fmtDay(Number(v))} formatter={(v: number | [number, number], name) => [Array.isArray(v) ? `${v[0].toFixed(1)} – ${v[1].toFixed(1)} mm` : `${v.toFixed(1)} mm`, name]} />
            <Legend {...LEGEND} />
            <Area name="95 % band" dataKey="smoothBand" fill={VIZ.series2} fillOpacity={0.2} stroke="none" {...NO_ANIMATION} />
            <Scatter name="Observed" dataKey="observed" fill={VIZ.series1} shape="circle" legendType="circle" {...NO_ANIMATION} />
            <Line name="GP smoothed" dataKey="smooth" stroke={VIZ.series2} strokeWidth={2} dot={false} {...NO_ANIMATION} />
          </ComposedChart>
        )}
      </ChartFrame>

      {data.sweep.length > 0 && (
        <table className="w-full text-[11px]" data-testid="backtest-sweep">
          <caption className="mb-1 text-left text-xs font-semibold text-slate-200">Forecast re-issued at each epoch of the window</caption>
          <thead className="text-slate-400">
            <tr>
              <th className="text-left font-normal">Issued</th>
              <th className="text-right font-normal">Lead</th>
              <th className="text-right font-normal">Estimate</th>
              <th className="text-right font-normal">Error</th>
              <th className="pl-2 text-left font-normal">95 %</th>
            </tr>
          </thead>
          <tbody className="font-mono text-slate-200">
            {data.sweep.map((s) => (
              <tr key={s.issued} className="border-t border-slate-800">
                <td className="whitespace-nowrap">{fmtDay(ms(s.issued))}</td>
                <td className="text-right">{num(s.lead_days, 0)} d</td>
                <td className="whitespace-nowrap pl-1 text-right">{s.estimate ? fmtDay(ms(s.estimate)) : '—'}</td>
                <td className="pl-1 text-right">{s.error_days === null ? '—' : `${s.error_days >= 0 ? '+' : ''}${s.error_days.toFixed(1)}`}</td>
                <td className="pl-2 font-sans">
                  <StatusBadge level={s.collapse_within_95 === null ? 'unknown' : s.collapse_within_95 ? 'good' : 'critical'} label={s.collapse_within_95 === null ? 'n/a' : s.collapse_within_95 ? 'Yes' : 'No'} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="text-[11px] leading-snug text-slate-500">{data.disclaimer}</p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Stress test
// --------------------------------------------------------------------------- //

function regimeLabel(code: number | null | undefined) {
  if (code === null || code === undefined) return '—';
  return REGIME_CLASSES.find((c) => c.value === code)?.label ?? String(code);
}

export function StressTestPanel({ dash }: { dash: Dashboard }) {
  const { selected, pick, setStressOverlay } = dash;
  const [intensity, setIntensity] = useState(0.5);
  const debounced = useDebounced(intensity, 400);
  const [showOverlay, setShowOverlay] = useState(true);
  const [state, setState] = useState<'idle' | 'running' | 'ready' | 'error'>('idle');
  const [data, setData] = useState<StressTestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const az = selected ? inArizona(selected) : false;
  const cell = pick ? { lat: pick.lat, lon: pick.lon } : null;
  const started = useRef(0);

  useEffect(() => {
    if (!selected || !az || !cell) return;
    const ctrl = new AbortController();
    started.current = Date.now();
    setState('running');
    api
      .stressTest(selected.id, cell.lat, cell.lon, debounced, ctrl.signal)
      .then((d) => {
        setData(d);
        setState('ready');
      })
      .catch((err) => {
        if (err instanceof Error && err.name === 'AbortError') return;
        setError(errorText(err));
        setState('error');
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, az, cell?.lat, cell?.lon, debounced]);

  useEffect(() => {
    setStressOverlay(showOverlay && data && state === 'ready' ? { matrix: data.regime_matrix, footprint: data.footprint, intensity: data.parameters.intensity } : null);
  }, [data, state, showOverlay, setStressOverlay]);
  useEffect(() => () => setStressOverlay(null), [setStressOverlay]);

  const dispRows = useMemo(
    () => (data ? data.times.map((t, i) => ({ t: ms(t), baseline: data.baseline_displacement_mm[i], scenario: data.scenario_displacement_mm[i] })) : []),
    [data],
  );
  const setbackRows = useMemo(
    () => (data ? data.setback.dates.map((t, i) => ({ t: ms(t), baseline: data.setback.baseline_d_crest_m[i], scenario: data.setback.scenario_d_crest_m[i] })) : []),
    [data],
  );

  const warning = (
    <p role="note" data-testid="stress-warning" className="rounded border-2 border-dashed border-amber-400 bg-amber-950/60 px-2 py-1.5 text-[11px] leading-snug text-amber-100">
      <span className="font-bold tracking-wider">SIMULATED HAZARD SCENARIO.</span> Synthetic pore-pressure seepage anomalies (ΔSAI, tertiary creep, shrinking pond setback) are
      injected into the cached products and re-run through the screening pipelines. Results are <strong>test overlays</strong>, not observations or forecasts.
    </p>
  );
  if (!selected) return <div className="space-y-2">{warning}<Empty>Select an Arizona facility (Sierrita or Mission) to run a stress test.</Empty></div>;
  if (!az)
    return (
      <div className="space-y-2">
        {warning}
        <Empty testId="stress-not-az">Stress tests are limited to Arizona (AZ) facilities; {selected.name} is outside Arizona.</Empty>
      </div>
    );

  return (
    <div className="space-y-2" data-testid="stress-test">
      {warning}
      {cell ? (
        <p className="text-[11px] text-slate-400">
          Scenario centred on the picked cell <span className="font-mono text-slate-200" data-testid="stress-cell">{cell.lat.toFixed(5)}, {cell.lon.toFixed(5)}</span> (click the map to move it).
        </p>
      ) : (
        <div className="space-y-1">
          <Empty testId="stress-needs-cell">Click the map (or use the Inspector) to pick the cell at the centre of the scenario.</Empty>
          <button type="button" className="rounded bg-slate-800 px-2 py-1 text-xs hover:bg-slate-700" onClick={() => dash.pickPixel(selected.longitude, selected.latitude)}>
            Use the facility centre
          </button>
        </div>
      )}
      <label className="block text-[11px] text-slate-400">
        Hazard intensity <span className="font-mono text-slate-100" data-testid="stress-intensity-label">{Math.round(intensity * 100)} %</span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={intensity}
          onChange={(e) => setIntensity(Number(e.target.value))}
          className="w-full accent-amber-400"
          aria-label="Hazard intensity"
          data-testid="stress-slider"
        />
        <span className="flex justify-between text-[10px] text-slate-500">
          <span>0 % baseline</span>
          <span>100 % severe</span>
        </span>
      </label>
      <label className="flex items-center gap-2 text-xs">
        <input type="checkbox" className="h-4 w-4 accent-amber-400" checked={showOverlay} onChange={(e) => setShowOverlay(e.target.checked)} data-testid="stress-overlay-toggle" />
        Show the simulated regimes as a test overlay on the map
      </label>
      <p className="text-[11px] text-slate-400" data-testid="stress-status" data-state={state}>
        {state === 'running' && 'Running Phase 3 (GP + inverse velocity) and Phase 5 coupling on the perturbed products…'}
        {state === 'ready' && data && `Scenario at ${Math.round(data.parameters.intensity * 100)} % computed.`}
        {state === 'error' && <span role="alert" className="text-red-300">Stress test failed: {error}</span>}
      </p>

      {data && (
        <>
          <p className="text-[11px] text-slate-400" data-testid="stress-parameters">
            ΔSAI +{data.parameters.delta_sai_max.toFixed(2)} (within {data.parameters.footprint_radius_m} m) · creep from {fmtDay(ms(data.parameters.creep_onset))} with
            a synthetic trend change {Math.round(data.parameters.creep_lead_days)} d after the last epoch · D_crest ramped towards {data.parameters.d_crest_target_m} m
          </p>
          <table className="w-full text-[11px]" data-testid="stress-compare">
            <thead className="text-slate-400">
              <tr>
                <th className="text-left font-normal" />
                <th className="text-left font-normal">Baseline</th>
                <th className="text-left font-normal">
                  <span className="rounded bg-amber-900/70 px-1 text-amber-100">SIMULATED</span>
                </th>
              </tr>
            </thead>
            <tbody className="text-slate-200">
              <tr className="border-t border-slate-800">
                <td className="text-slate-400">SAI</td>
                <td className="font-mono">{num(data.baseline.sai, 3)}</td>
                <td className="font-mono" data-testid="stress-sai">{num(data.scenario.sai, 3)}</td>
              </tr>
              <tr className="border-t border-slate-800">
                <td className="text-slate-400">Kinematics</td>
                <td>{data.baseline.kinematic_status ?? '—'}</td>
                <td data-testid="stress-status-kin">{data.scenario.kinematic_status ?? '—'}</td>
              </tr>
              <tr className="border-t border-slate-800">
                <td className="text-slate-400">Regime</td>
                <td>{regimeLabel(data.baseline.regime)}</td>
                <td data-testid="stress-regime">{regimeLabel(data.scenario.regime)}</td>
              </tr>
              <tr className="border-t border-slate-800">
                <td className="text-slate-400">Facility hazard</td>
                <td>{data.baseline_hazard_level}</td>
                <td data-testid="stress-hazard">{data.scenario_hazard_level}</td>
              </tr>
            </tbody>
          </table>
          <ChartFrame title="LOS displacement: baseline vs SIMULATED creep (mm)" testId="chart-stress-displacement" height={150} caption="SIMULATED series: synthetic tertiary creep added to the cached DISP-S1 series of this cell.">
            {(width) => (
              <LineChart width={width} height={150} data={dispRows} margin={{ top: 4, right: 10, bottom: 0, left: -12 }}>
                <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
                <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={fmtMonth} minTickGap={24} {...AXIS} />
                <YAxis width={48} {...AXIS} />
                <Tooltip {...TOOLTIP} labelFormatter={(v) => fmtDay(Number(v))} formatter={(v: number, name) => [`${v.toFixed(1)} mm`, name]} />
                <Legend {...LEGEND} />
                <Line name="Baseline" dataKey="baseline" stroke={VIZ.series1} strokeWidth={1.5} dot={false} {...NO_ANIMATION} />
                <Line name="SIMULATED" dataKey="scenario" stroke={VIZ.series2} strokeWidth={2} strokeDasharray="5 3" dot={false} {...NO_ANIMATION} />
              </LineChart>
            )}
          </ChartFrame>
          {setbackRows.length > 0 && (
            <ChartFrame title="Pond-to-crest setback: baseline vs SIMULATED (m)" testId="chart-stress-setback" height={140}>
              {(width) => (
                <LineChart width={width} height={140} data={setbackRows} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
                  <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
                  <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={fmtDay} padding={{ left: 16, right: 16 }} {...AXIS} />
                  <YAxis domain={[0, 'auto']} width={44} {...AXIS} />
                  <ReferenceLine y={15} stroke={STATUS.critical.color} strokeDasharray="4 3" label={{ value: '15 m proximity alert', fill: VIZ.axis, fontSize: 9, position: 'insideTopRight' }} />
                  <Tooltip {...TOOLTIP} labelFormatter={(v) => fmtDay(Number(v))} formatter={(v: number, name) => [`${v.toFixed(1)} m`, name]} />
                  <Legend {...LEGEND} />
                  <Line name="Baseline" dataKey="baseline" stroke={VIZ.series1} strokeWidth={1.5} {...NO_ANIMATION} />
                  <Line name="SIMULATED" dataKey="scenario" stroke={VIZ.series2} strokeWidth={2} strokeDasharray="5 3" {...NO_ANIMATION} />
                </LineChart>
              )}
            </ChartFrame>
          )}
          {data.scenario_series.fukuzono?.crossing_estimate && (
            <p className="text-[11px] text-slate-400">
              SIMULATED inverse-velocity trend change: {fmtDay(ms(data.scenario_series.fukuzono.crossing_estimate))} (a property of the injected creep, not a forecast).
            </p>
          )}
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Panel
// --------------------------------------------------------------------------- //

export default function ScenarioCtrl({ dash }: { dash: Dashboard }) {
  const [view, setView] = useState<'backtest' | 'stress'>('backtest');
  return (
    <div className="space-y-3" data-testid="scenario-ctrl">
      <div role="tablist" aria-label="Scenarios" className="grid grid-cols-2 gap-1 rounded-md bg-slate-900 p-1">
        {(
          [
            ['backtest', 'Brumadinho backtest'],
            ['stress', 'Stress-test simulator'],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={view === id}
            data-testid={`scenario-tab-${id}`}
            onClick={() => setView(id)}
            className={`rounded px-2 py-1 text-xs ${view === id ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
          >
            {label}
          </button>
        ))}
      </div>
      {view === 'backtest' ? <BacktestPanel dash={dash} /> : <StressTestPanel dash={dash} />}
    </div>
  );
}
