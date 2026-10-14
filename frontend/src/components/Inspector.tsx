/**
 * Double Inspector: spectroscopy and kinematics of the picked cell, plus the facility's
 * pond-to-crest setback history with GISTM screening checks.
 *
 * A cell is picked by clicking the map (MapViewer -> dash.pickPixel) or by entering coordinates
 * here; both query GET /api/facilities/{id}/pixel. The hydrology tab reads the facility-level
 * GET /api/facilities/{id}/dashboard summary.
 */
import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  LabelList,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { AXIS, ChartFrame, Empty, LEGEND, NO_ANIMATION, StatTile, StatusBadge, TOOLTIP, fmtDay, fmtMonth, num } from '@/components/ChartKit';
import type { Dashboard } from '@/hooks/useDashboard';
import type { KinematicSeries, PixelResponse, SetbackSummary, Spectrum } from '@/utils/api';
import {
  ALERT_TEXT,
  RMSE_THRESHOLD,
  SAI_THRESHOLD,
  STATUS,
  VIZ,
  abundanceGroups,
  coherenceCheck,
  fukuzonoRows,
  hasSeries,
  kinematicRows,
  kinematicStatusText,
  ms,
  rmseLevel,
  saiLevel,
  setbackChecks,
  setbackRows,
  spectrumRows,
  type KinematicRow,
  type StatusLevel,
} from '@/utils/inspector';
import { formatDate } from '@/utils/provenance';

// --------------------------------------------------------------------------- //
// Coordinate picker
// --------------------------------------------------------------------------- //

export function CoordinatePicker({ dash }: { dash: Dashboard }) {
  const { selected, pick } = dash;
  const [lat, setLat] = useState('');
  const [lon, setLon] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (pick) {
      setLat(pick.lat.toFixed(6));
      setLon(pick.lon.toFixed(6));
    } else if (selected) {
      setLat(selected.latitude.toFixed(6));
      setLon(selected.longitude.toFixed(6));
    }
    setError(null);
    // Only a new coordinate or facility resets the inputs (not the pick's loading -> ready update).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pick?.lat, pick?.lon, selected?.id]);

  if (!selected) return null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const la = Number(lat);
    const lo = Number(lon);
    if (!Number.isFinite(la) || !Number.isFinite(lo) || lat.trim() === '' || lon.trim() === '') {
      setError('Enter a latitude and longitude in decimal degrees.');
      return;
    }
    if (lo < selected.bbox_xmin || lo > selected.bbox_xmax || la < selected.bbox_ymin || la > selected.bbox_ymax) {
      setError(`Outside the ${selected.name} footprint (${selected.bbox_ymin}–${selected.bbox_ymax} N, ${selected.bbox_xmin}–${selected.bbox_xmax} E).`);
      return;
    }
    setError(null);
    dash.pickPixel(lo, la);
  };
  return (
    <form onSubmit={submit} className="space-y-1" aria-label="Coordinate picker" data-testid="coordinate-picker">
      <p className="text-[11px] text-slate-400">Click the map inside the footprint, or enter a coordinate:</p>
      <div className="flex items-end gap-2">
        <label className="min-w-0 flex-1 text-[11px] text-slate-400">
          Latitude
          <input
            value={lat}
            onChange={(e) => setLat(e.target.value)}
            inputMode="decimal"
            className="mt-0.5 w-full rounded border border-slate-700 bg-slate-900 px-1.5 py-1 font-mono text-xs text-slate-100"
            data-testid="input-lat"
          />
        </label>
        <label className="min-w-0 flex-1 text-[11px] text-slate-400">
          Longitude
          <input
            value={lon}
            onChange={(e) => setLon(e.target.value)}
            inputMode="decimal"
            className="mt-0.5 w-full rounded border border-slate-700 bg-slate-900 px-1.5 py-1 font-mono text-xs text-slate-100"
            data-testid="input-lon"
          />
        </label>
        <button type="submit" className="rounded bg-sky-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-sky-600">
          Inspect
        </button>
      </div>
      {error && (
        <p role="alert" className="text-[11px] text-red-300">
          {error}
        </p>
      )}
    </form>
  );
}

// --------------------------------------------------------------------------- //
// Spectroscopy
// --------------------------------------------------------------------------- //

function SpectrumChart({ spectrum }: { spectrum: Spectrum }) {
  const rows = spectrumRows(spectrum);
  const excluded = spectrum.active.filter((a) => !a).length;
  return (
    <ChartFrame
      title="VSWIR reflectance (285 bands)"
      testId="chart-spectrum"
      height={190}
      caption={
        <>
          Measured spectrum of the nearest 60 m cell and its NNLS fit (M·f). {excluded} water-vapour channels (
          {spectrum.excluded_windows_nm.map(([a, b]) => `${a}–${b} nm`).join(', ')}) are excluded from the fit and left as gaps.
        </>
      }
    >
      {(width) => (
        <LineChart width={width} height={190} data={rows} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
          <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
          {spectrum.excluded_windows_nm.map(([a, b]) => (
            <ReferenceArea key={a} x1={a} x2={b} fill={VIZ.excluded} fillOpacity={0.25} strokeOpacity={0} ifOverflow="extendDomain" label={{ value: 'H₂O', fill: VIZ.axis, fontSize: 9, position: 'insideTop' }} />
          ))}
          <XAxis dataKey="wl" type="number" domain={[350, 2550]} ticks={[500, 1000, 1500, 2000, 2500]} unit=" nm" {...AXIS} />
          <YAxis domain={[0, 'auto']} tickFormatter={(v: number) => v.toFixed(2)} width={44} {...AXIS} />
          <Tooltip {...TOOLTIP} labelFormatter={(v) => `${Number(v).toFixed(1)} nm`} formatter={(v: number, name) => [v === null ? 'excluded' : v.toFixed(4), name]} />
          <Legend {...LEGEND} />
          <Line name="Measured" dataKey="measured" stroke={VIZ.series1} strokeWidth={1.5} dot={false} connectNulls={false} {...NO_ANIMATION} />
          <Line name="NNLS fit" dataKey="modelled" stroke={VIZ.series2} strokeWidth={1.5} strokeDasharray="4 3" dot={false} connectNulls={false} {...NO_ANIMATION} />
        </LineChart>
      )}
    </ChartFrame>
  );
}

/** Value label right of a horizontal bar (a custom renderer: Recharts would wrap it to the bar width). */
function BarValueLabel(props: { x?: number | string; y?: number | string; width?: number | string; height?: number | string; value?: number | string | null }) {
  const x = Number(props.x ?? 0) + Number(props.width ?? 0) + 4;
  const y = Number(props.y ?? 0) + Number(props.height ?? 0) / 2;
  const v = props.value === null || props.value === undefined ? null : Number(props.value);
  return (
    <text x={x} y={y} dy={3.5} fill={VIZ.text} fontSize={10}>
      {v === null || !Number.isFinite(v) ? '—' : `${v.toFixed(1)} %`}
    </text>
  );
}

function AbundanceChart({ abundance, note }: { abundance: Record<string, number | null>; note: string }) {
  const groups = abundanceGroups(abundance).map((g) => ({ ...g, pct: g.value === null ? null : Math.round(g.value * 1000) / 10 }));
  if (groups.every((g) => g.value === null)) return <Empty testId="abundance-empty">No unmixing result at this cell (no data or vegetated).</Empty>;
  return (
    <ChartFrame title="Endmember abundance (NNLS fraction)" testId="chart-abundance" height={150} caption={note}>
      {(width) => (
        <BarChart width={width} height={150} data={groups} layout="vertical" margin={{ top: 0, right: 48, bottom: 0, left: 4 }} barCategoryGap={4}>
          <XAxis type="number" domain={[0, 100]} hide />
          <YAxis type="category" dataKey="label" width={Math.min(170, width * 0.48)} {...AXIS} axisLine={false} />
          <Tooltip {...TOOLTIP} cursor={{ fill: '#1e293b' }} formatter={(v: number) => [`${v.toFixed(1)} %`, 'fraction']} />
          <Bar dataKey="pct" fill={VIZ.series1} radius={[0, 4, 4, 0]} {...NO_ANIMATION}>
            <LabelList dataKey="pct" content={BarValueLabel} />
          </Bar>
        </BarChart>
      )}
    </ChartFrame>
  );
}

export function SpectroscopyPanel({ data }: { data: PixelResponse }) {
  const s = data.spectral;
  if (!s) return <Empty testId="spectral-empty">No spectral product at this cell{data.unavailable.spectral ? `: ${data.unavailable.spectral}` : '.'}</Empty>;
  const sp = s.spectrum ?? null;
  const rmse = sp?.rmse ?? s.rmse;
  const sai = sp?.sai ?? s.sai;
  return (
    <div className="space-y-2" data-testid="panel-spectral">
      <div className="grid grid-cols-2 gap-2">
        <StatTile
          testId="pixel-sai"
          label={`Seepage Alteration Index (threshold ${SAI_THRESHOLD})`}
          value={num(sai, 3)}
          sub={<StatusBadge level={saiLevel(sai)} label={saiLevel(sai) === 'serious' ? 'Above threshold' : saiLevel(sai) === 'good' ? 'Below threshold' : 'Not computed'} />}
        />
        <StatTile
          testId="pixel-rmse"
          label={`Unmixing error RMSE (limit ${RMSE_THRESHOLD})`}
          value={num(rmse, 4)}
          sub={<StatusBadge level={rmseLevel(rmse)} label={rmseLevel(rmse) === 'warning' ? 'Low-confidence fit' : rmseLevel(rmse) === 'good' ? 'Fit accepted' : 'No fit'} />}
        />
      </div>
      {s.observability && s.observability !== 'observable' && (
        <p role="alert" className="rounded border border-amber-500/40 bg-amber-950/40 px-2 py-1 text-[11px] text-amber-100">
          Spectral observability: {s.observability}. SAI is not interpreted for this cell.
        </p>
      )}
      {sp ? <SpectrumChart spectrum={sp} /> : <Empty testId="spectrum-empty">Spectrum unavailable: {s.spectrum_unavailable ?? 'not in this product'}.</Empty>}
      <AbundanceChart
        abundance={sp?.abundance ?? s.abundance}
        note={sp ? 'Fractions of the same cell as the spectrum (sum ≤ 1).' : 'Fractions interpolated between the 4 nearest cells.'}
      />
      <p className="text-[11px] text-slate-500">
        {s.source.synthetic_or_mock ? 'SIMULATED input · ' : ''}
        {s.source.label}
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Kinematics
// --------------------------------------------------------------------------- //

function SeriesChart({
  rows,
  title,
  testId,
  value,
  bandKey,
  unit,
  observed,
}: {
  rows: KinematicRow[];
  title: string;
  testId: string;
  value: keyof KinematicRow;
  bandKey: keyof KinematicRow;
  unit: string;
  observed?: boolean;
}) {
  return (
    <ChartFrame title={title} testId={testId} height={150}>
      {(width) => (
        <ComposedChart width={width} height={150} data={rows} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
          <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={fmtMonth} minTickGap={24} {...AXIS} />
          <YAxis width={48} tickFormatter={(v: number) => v.toFixed(Math.abs(v) < 10 ? 1 : 0)} {...AXIS} />
          <ReferenceLine y={0} stroke={VIZ.muted} />
          <Tooltip {...TOOLTIP} labelFormatter={(v) => fmtDay(Number(v))} formatter={(v: number | [number, number], name) => [Array.isArray(v) ? `${v[0].toFixed(2)} – ${v[1].toFixed(2)} ${unit}` : `${v.toFixed(2)} ${unit}`, name]} />
          <Legend {...LEGEND} />
          <Area name="95 % band (±2σ)" dataKey={bandKey as string} fill={VIZ.series2} fillOpacity={0.2} stroke="none" connectNulls={false} {...NO_ANIMATION} />
          {observed && <Scatter name="Observed" dataKey="observed" fill={VIZ.series1} shape="circle" legendType="circle" {...NO_ANIMATION} />}
          <Line name="GP smoothed" dataKey={value as string} stroke={VIZ.series2} strokeWidth={2} dot={false} connectNulls={false} {...NO_ANIMATION} />
        </ComposedChart>
      )}
    </ChartFrame>
  );
}

function FukuzonoChart({ k }: { k: KinematicSeries }) {
  const fit = k.fukuzono!;
  const rows = fukuzonoRows(k, fit);
  const lo = fit.crossing_lower ? ms(fit.crossing_lower) : null;
  const hi = fit.crossing_upper ? ms(fit.crossing_upper) : null;
  const est = fit.crossing_estimate ? ms(fit.crossing_estimate) : null;
  // Explicit domain: the scatter series carry their own data, so derive the extent from every row.
  const domain: [number, number] = [rows[0]?.t ?? 0, Math.max(rows.at(-1)?.t ?? 0, hi ?? 0)];
  return (
    <ChartFrame
      title="Inverse velocity Λ(t) = |v_LOS|⁻¹ → 0 (Fukuzono)"
      testId="chart-fukuzono"
      height={180}
      caption={
        <>
          <span data-testid="fukuzono-label" className="font-semibold text-slate-200">
            {fit.label}.
          </span>{' '}
          {est !== null && (
            <>
              Estimated change in velocity: <span className="font-mono">{fmtDay(est)}</span>
              {lo !== null && hi !== null && (
                <>
                  {' '}
                  (95 %: <span className="font-mono">{fmtDay(lo)}</span> – <span className="font-mono">{fmtDay(hi)}</span>)
                </>
              )}
              .{' '}
            </>
          )}
          Filled points are the {fit.window_times.length} epochs in the Bayesian fit, hollow points earlier epochs; slope {fit.slope_yr_mm_per_yr.toExponential(2)} ± {(2 * fit.slope_std).toExponential(1)} yr/mm per yr (2σ)
          {fit.slope_significant ? '' : ', not significant at 2σ'}. Phase 3 tests show these estimates run late.
        </>
      }
    >
      {(width) => (
        <ComposedChart width={width} height={180} data={rows} margin={{ top: 14, right: 8, bottom: 0, left: -6 }}>
          <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
          {lo !== null && hi !== null && (
            <ReferenceArea x1={lo} x2={hi} fill={VIZ.series2} fillOpacity={0.12} strokeOpacity={0} label={{ value: '95 %', fill: VIZ.axis, fontSize: 9, position: 'insideTop' }} />
          )}
          <XAxis dataKey="t" type="number" scale="time" domain={domain} tickFormatter={fmtMonth} minTickGap={24} {...AXIS} />
          <YAxis width={54} tickFormatter={(v: number) => v.toFixed(3)} {...AXIS} />
          <ReferenceLine y={0} stroke={VIZ.muted} label={{ value: 'Λ = 0', fill: VIZ.axis, fontSize: 9, position: 'insideBottomLeft' }} />
          {est !== null && <ReferenceLine x={est} stroke={VIZ.series2} strokeDasharray="2 2" />}
          <Tooltip {...TOOLTIP} labelFormatter={(v) => fmtDay(Number(v))} formatter={(v: number | [number, number], name) => [Array.isArray(v) ? `${v[0].toFixed(4)} – ${v[1].toFixed(4)} yr/mm` : `${v.toFixed(4)} yr/mm`, name]} />
          <Legend {...LEGEND} />
          <Area name="Prediction band (95 %)" dataKey="fitBand" fill={VIZ.series2} fillOpacity={0.2} stroke="none" connectNulls {...NO_ANIMATION} />
          <Scatter name="Earlier Λ" data={rows.filter((r) => r.context !== null)} dataKey="context" fill="none" stroke={VIZ.series1} shape="circle" legendType="none" {...NO_ANIMATION} />
          <Scatter name="Fitted Λ" data={rows.filter((r) => r.lambda !== null)} dataKey="lambda" fill={VIZ.series1} shape="circle" legendType="circle" {...NO_ANIMATION} />
          <Line name="Regression" dataKey="fit" stroke={VIZ.series2} strokeWidth={2} strokeDasharray="5 3" dot={false} connectNulls {...NO_ANIMATION} />
        </ComposedChart>
      )}
    </ChartFrame>
  );
}

export function KinematicsPanel({ data }: { data: PixelResponse }) {
  const k = data.kinematics;
  if (!k) return <Empty testId="kinematics-empty">No displacement series at this cell{data.unavailable.kinematics ? `: ${data.unavailable.kinematics}` : '.'}</Empty>;
  const rows = kinematicRows(k);
  const coh = coherenceCheck(k);
  const anyObserved = rows.some((r) => r.observed !== null);
  return (
    <div className="space-y-2" data-testid="panel-kinematics">
      <div className="grid grid-cols-2 gap-2">
        <StatTile
          testId="pixel-coherence"
          label={`Temporal coherence γ (min ${coh.threshold})`}
          value={num(coh.gamma, 2)}
          sub={<StatusBadge level={coh.gamma === null ? 'unknown' : coh.low ? 'critical' : 'good'} label={coh.low ? 'Decorrelated' : coh.gamma === null ? 'Unknown' : 'Coherent'} />}
        />
        <StatTile label="Kinematic status" value={<span className="text-xs">{k.status ?? '—'}</span>} />
      </div>
      {coh.low && (
        <p role="alert" data-testid="coherence-alert" className="rounded border border-red-500/50 bg-red-950/50 px-2 py-1.5 text-[11px] text-red-100">
          <StatusBadge level="critical" label="GP fit unreliable" /> {coh.message}
        </p>
      )}
      {!anyObserved ? (
        <Empty testId="displacement-empty">The displacement series at this cell is empty.</Empty>
      ) : (
        <SeriesChart rows={rows} title="Cumulative LOS displacement (mm)" testId="chart-displacement" value="smooth" bandKey="smoothBand" unit="mm" observed />
      )}
      {hasSeries(rows, 'velocity') ? (
        <SeriesChart rows={rows} title="GP velocity (mm/yr)" testId="chart-velocity" value="velocity" bandKey="velocityBand" unit="mm/yr" />
      ) : (
        <Empty testId="velocity-empty">No GP fit at this cell{coh.low ? ' (low coherence)' : ''}: velocity and acceleration are not modelled.</Empty>
      )}
      {hasSeries(rows, 'acceleration') && (
        <SeriesChart rows={rows} title="GP acceleration (mm/yr²)" testId="chart-acceleration" value="acceleration" bandKey="accelerationBand" unit="mm/yr²" />
      )}
      {k.fukuzono ? <FukuzonoChart k={k} /> : <Empty testId="fukuzono-empty">No inverse-velocity trend: {kinematicStatusText(k.status)}</Empty>}
      {k.disclaimer && <p className="text-[11px] leading-snug text-slate-500">{k.disclaimer}</p>}
      <p className="text-[11px] text-slate-500">
        {k.source.synthetic_or_mock ? 'SIMULATED input · ' : ''}
        {k.source.label}
        {k.match.note ? ` · ${k.match.note}` : ''}
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Hydrology
// --------------------------------------------------------------------------- //

function SetbackChart({ setback }: { setback: SetbackSummary }) {
  const rows = setbackRows(setback);
  const proximity = setback.thresholds?.proximity_alert_m ?? 15;
  const max = Math.max(proximity * 2, ...rows.map((r) => r.dCrest ?? 0));
  return (
    <ChartFrame
      title="Pond-to-crest setback D_crest(t) (m)"
      testId="chart-setback"
      height={170}
      caption="Shortest distance from DSWx open water to the crest line at each epoch. Marker colour follows the alert level in the table below."
    >
      {(width) => (
        <LineChart width={width} height={170} data={rows} margin={{ top: 10, right: 12, bottom: 0, left: -12 }}>
          <CartesianGrid stroke={VIZ.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={fmtDay} padding={{ left: 16, right: 16 }} minTickGap={24} {...AXIS} />
          <YAxis domain={[0, Math.ceil((max * 1.15) / 10) * 10]} allowDecimals={false} width={44} {...AXIS} />
          <ReferenceLine y={proximity} stroke={STATUS.critical.color} strokeDasharray="4 3" label={{ value: `${proximity} m proximity alert`, fill: VIZ.axis, fontSize: 9, position: 'insideTopRight' }} />
          <Tooltip {...TOOLTIP} labelFormatter={(v) => fmtDay(Number(v))} formatter={(v: number) => [`${v.toFixed(1)} m`, 'D_crest']} />
          <Line
            name="D_crest"
            dataKey="dCrest"
            stroke={VIZ.series1}
            strokeWidth={2}
            connectNulls={false}
            dot={(p: { cx?: number; cy?: number; payload?: { level: StatusLevel; t: number } }) => (
              <circle key={p.payload?.t} cx={p.cx} cy={p.cy} r={5} fill={STATUS[p.payload?.level ?? 'unknown'].color} stroke="#0f172a" strokeWidth={2} />
            )}
            {...NO_ANIMATION}
          />
        </LineChart>
      )}
    </ChartFrame>
  );
}

export function HydrologyPanel({ dash }: { dash: Dashboard }) {
  const { summary } = dash;
  if (summary.state === 'loading' || summary.state === 'idle') return <Empty>Loading the setback history…</Empty>;
  if (summary.state === 'error' || !summary.data) return <Empty testId="hydrology-error">Setback history unavailable: {summary.error}</Empty>;
  const setback = summary.data.hydrological_setback;
  const checks = setbackChecks(setback);
  const issues = summary.data.gistm_screening?.candidate_issues ?? [];
  const rows = setbackRows(setback);
  return (
    <div className="space-y-2" data-testid="panel-hydrology">
      {setback.available && rows.length ? (
        <>
          <SetbackChart setback={setback} />
          <table className="w-full text-[11px]" data-testid="setback-table">
            <thead className="text-slate-400">
              <tr>
                <th className="text-left font-normal">Epoch</th>
                <th className="text-right font-normal">D_crest</th>
                <th className="whitespace-nowrap pl-2 text-right font-normal" title="change since the first epoch (m)">Δ first</th>
                <th className="whitespace-nowrap pl-2 text-right font-normal" title="change since the previous epoch (m)">Δ prev</th>
                <th className="pl-2 text-left font-normal">Alert</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
              {rows.map((r) => (
                <tr key={r.date} className="border-t border-slate-800 align-top">
                  <td className="whitespace-nowrap pr-1">{r.date}</td>
                  <td className="text-right">{num(r.dCrest, 1)}</td>
                  <td className="pl-2 text-right">{num(r.epoch.delta_vs_baseline_m, 1)}</td>
                  <td className="pl-2 text-right">{num(r.epoch.delta_vs_previous_m, 1)}</td>
                  <td className="pl-2 font-sans">
                    <StatusBadge level={r.level} label={r.level === 'unknown' ? r.epoch.status : r.epoch.alerts.length ? `${r.epoch.alerts.length} alert${r.epoch.alerts.length > 1 ? 's' : ''}` : 'None'} />
                    {r.epoch.alerts.map((a) => (
                      <span key={a} className="block text-[10px] text-slate-400">
                        {ALERT_TEXT[a] ?? a}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <Empty testId="setback-empty">No setback history for this facility{setback.reason ? `: ${setback.reason}` : '.'}</Empty>
      )}

      <section aria-labelledby="gistm-checks" className="rounded-md border border-slate-800 bg-slate-900 p-2" data-testid="gistm-checks">
        <h3 id="gistm-checks" className="mb-1 text-xs font-semibold text-slate-200">
          GISTM screening checks
        </h3>
        <ul className="space-y-1.5">
          {checks.map((c) => (
            <li key={c.id} data-check={c.id} data-level={c.level} className="text-[11px]">
              <div className="flex items-start justify-between gap-2">
                <span className="text-slate-200">{c.check}</span>
                <StatusBadge level={c.level} />
              </div>
              <span className="text-slate-500">
                {c.principle} · {c.detail}
              </span>
            </li>
          ))}
        </ul>
        {issues.length > 0 && (
          <>
            <h4 className="mb-1 mt-2 text-[11px] font-semibold text-slate-300">Facility items flagged by the inspection tools</h4>
            <ul className="list-disc space-y-1 pl-4 text-[11px] text-slate-300" data-testid="gistm-issues">
              {issues.map((i) => (
                <li key={i.issue}>
                  <span className="text-slate-400">{i.gistm_principle.split(':')[0]}:</span> {i.issue}
                </li>
              ))}
            </ul>
          </>
        )}
        <p className="mt-2 text-[10px] text-slate-500">Screening aid for a qualified reviewer, not a GISTM compliance determination. Principles are paraphrased from GISTM (2020).</p>
      </section>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Inspector
// --------------------------------------------------------------------------- //

type Tab = 'spectral' | 'kinematics' | 'hydrology';
const TABS: { id: Tab; label: string }[] = [
  { id: 'spectral', label: 'Spectroscopy' },
  { id: 'kinematics', label: 'Kinematics' },
  { id: 'hydrology', label: 'Hydrology' },
];

function CellSummary({ data }: { data: PixelResponse }) {
  const c = data.classification;
  return (
    <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-0.5 text-[11px]">
      {c && (
        <>
          <dt className="text-slate-400">Regime</dt>
          <dd data-testid="pixel-regime">
            {c.regime} · {c.regime_meaning}
            {c.low_confidence && <span className="ml-1 text-amber-300">(low confidence)</span>}
          </dd>
          {c.failure_time_estimate && (
            <>
              <dt className="text-slate-400">Trend change</dt>
              <dd>
                {formatDate(c.failure_time_estimate)} [95 %: {formatDate(c.failure_time_lower)} – {formatDate(c.failure_time_upper)}]{' '}
                <span className="text-slate-500">(estimate, not a failure forecast)</span>
              </dd>
            </>
          )}
        </>
      )}
      {Object.entries(data.unavailable).map(([k, v]) => (
        <div key={k} className="col-span-2 text-slate-500">
          {k}: {v}
        </div>
      ))}
    </dl>
  );
}

export default function Inspector({ dash }: { dash: Dashboard }) {
  const [tab, setTab] = useState<Tab>('spectral');
  const { pick, selected } = dash;
  if (!selected) return <p className="text-sm text-slate-400">Select a facility to inspect its cells.</p>;
  const needsCell = tab !== 'hydrology';
  return (
    <div className="space-y-2" data-testid="inspector">
      <CoordinatePicker dash={dash} />
      {pick && (
        <div data-testid="pixel-panel" className="space-y-1 rounded-md border border-slate-800 px-2 py-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="font-mono" data-testid="pixel-coords">
              {pick.lat.toFixed(5)}, {pick.lon.toFixed(5)}
            </span>
            <button type="button" className="text-[11px] text-slate-400 underline" onClick={dash.clearPick}>
              Clear
            </button>
          </div>
          {pick.state === 'loading' && <p className="text-[11px] text-slate-400">Loading cell…</p>}
          {pick.state === 'error' && (
            <p role="alert" className="text-[11px] text-red-300">
              {pick.error}
            </p>
          )}
          {pick.data && <CellSummary data={pick.data} />}
        </div>
      )}
      <div role="tablist" aria-label="Inspector panels" className="flex gap-1 border-b border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            id={`tab-${t.id}`}
            data-testid={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`tabpanel-${t.id}`}
            onClick={() => setTab(t.id)}
            className={`-mb-px border-b-2 px-2 py-1 text-xs ${tab === t.id ? 'border-sky-400 text-slate-100' : 'border-transparent text-slate-400 hover:text-slate-200'}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`tabpanel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {needsCell && !pick && <Empty testId="inspector-prompt">Pick a cell to plot its {tab === 'spectral' ? 'spectrum' : 'displacement history'}.</Empty>}
        {needsCell && pick?.state === 'loading' && <Empty>Loading…</Empty>}
        {needsCell && pick?.state === 'error' && <Empty>Cell could not be loaded.</Empty>}
        {tab === 'spectral' && pick?.data && <SpectroscopyPanel data={pick.data} />}
        {tab === 'kinematics' && pick?.data && <KinematicsPanel data={pick.data} />}
        {tab === 'hydrology' && <HydrologyPanel dash={dash} />}
      </div>
    </div>
  );
}
