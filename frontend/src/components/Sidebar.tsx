/**
 * Dashboard controls: active facility picker, overlay matrix toggles, the OPERA epoch timeline and
 * the selected-pixel panel (filled by clicking the map).
 */
import type { Dashboard } from '@/hooks/useDashboard';
import { ALERT_COLORS, ALERT_LABELS, alertColorName } from '@/utils/facilities';
import { OVERLAYS, type OverlayDef } from '@/utils/overlays';
import { formatDate } from '@/utils/provenance';

type SidebarProps = { dash: Dashboard; className?: string };

function Dot({ hazard }: { hazard: Parameters<typeof alertColorName>[0] }) {
  const name = alertColorName(hazard);
  const [r, g, b] = ALERT_COLORS[name];
  return (
    <span
      aria-hidden
      data-alert={name}
      className="inline-block h-2.5 w-2.5 shrink-0 rounded-full ring-1 ring-black/30"
      style={{ backgroundColor: `rgb(${r},${g},${b})` }}
    />
  );
}

function Section({ title, children, id }: { title: string; children: React.ReactNode; id: string }) {
  return (
    <section aria-labelledby={id} className="border-b border-slate-800 px-4 py-3">
      <h2 id={id} className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
        {title}
      </h2>
      {children}
    </section>
  );
}

export function FacilityPicker({ dash }: { dash: Dashboard }) {
  const { facilities, latestAlerts, registryState, registryError, selectedId, selectFacility } = dash;
  if (registryState === 'loading') return <p className="text-sm text-slate-400">Loading facilities…</p>;
  if (registryState === 'error')
    return (
      <div role="alert" className="rounded border border-red-500/40 bg-red-950/40 p-2 text-sm text-red-200">
        Backend unavailable: {registryError}
        <button type="button" className="ml-2 underline" onClick={() => void dash.reloadRegistry()}>
          Retry
        </button>
      </div>
    );
  return (
    <div className="space-y-2">
      <label htmlFor="facility-select" className="sr-only">
        Active facility
      </label>
      <select
        id="facility-select"
        data-testid="facility-select"
        className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm"
        value={selectedId ?? ''}
        onChange={(e) => selectFacility(e.target.value || null)}
      >
        <option value="">Global overview</option>
        {facilities.map((f) => (
          <option key={f.id} value={f.id}>
            {f.name}
          </option>
        ))}
      </select>
      <ul className="space-y-1" aria-label="Facilities by alert status">
        {facilities.map((f) => {
          const alert = latestAlerts[f.id];
          const active = f.id === selectedId;
          return (
            <li key={f.id}>
              <button
                type="button"
                aria-pressed={active}
                onClick={() => selectFacility(active ? null : f.id)}
                className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm transition-colors ${
                  active ? 'bg-sky-900/60 ring-1 ring-sky-500' : 'hover:bg-slate-800'
                }`}
              >
                <Dot hazard={alert?.hazard_level} />
                <span className="min-w-0 flex-1 truncate">{f.name}</span>
                <span className="shrink-0 text-xs text-slate-400">{f.country}</span>
              </button>
            </li>
          );
        })}
      </ul>
      <p className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-400">
        {(['green', 'yellow', 'red', 'unknown'] as const).map((c) => (
          <span key={c} className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: `rgb(${ALERT_COLORS[c].join(',')})` }} />
            {ALERT_LABELS[c]}
          </span>
        ))}
      </p>
    </div>
  );
}

function OverlayToggle({ def, dash }: { def: OverlayDef; dash: Dashboard }) {
  const { selectedId, available, layerInfoState, layerInfo, activeOverlays, toggleOverlay, matrices } = dash;
  const checked = activeOverlays.includes(def.id);
  const known = layerInfoState === 'ready';
  const isAvailable = !selectedId || !known || available.has(def.layer);
  const slot = matrices[def.id];
  const reason = layerInfo.find((l) => l.layer === def.layer)?.detail ?? undefined;
  const status = !selectedId ? null : !isAvailable ? 'n/a' : slot?.state === 'loading' ? 'loading' : slot?.state === 'error' ? 'error' : null;
  return (
    <li>
      <label
        className={`flex items-start gap-2 rounded px-1 py-1 text-sm ${isAvailable ? 'cursor-pointer hover:bg-slate-800' : 'cursor-not-allowed opacity-50'}`}
        title={isAvailable ? def.description : `Not available: ${reason ?? 'no product'}`}
      >
        <input
          type="checkbox"
          className="mt-0.5 h-4 w-4 accent-sky-500"
          data-testid={`toggle-${def.id}`}
          checked={checked}
          disabled={!isAvailable}
          onChange={() => toggleOverlay(def.id)}
        />
        <span className="min-w-0 flex-1">
          <span className="block leading-tight">{def.label}</span>
          <span className="block text-[11px] leading-tight text-slate-400">{def.description}</span>
        </span>
        {status && (
          <span data-testid={`status-${def.id}`} className="shrink-0 text-[10px] uppercase text-slate-400">
            {status}
          </span>
        )}
      </label>
    </li>
  );
}

export function LayerToggles({ dash }: { dash: Dashboard }) {
  const groups: [OverlayDef['group'], string][] = [
    ['screening', 'Screening matrices'],
    ['quality', 'Data-quality matrices'],
  ];
  return (
    <div className="space-y-3">
      {groups.map(([group, title]) => (
        <fieldset key={group}>
          <legend className="mb-1 text-[11px] font-medium text-slate-300">{title}</legend>
          <ul className="space-y-0.5">
            {OVERLAYS.filter((o) => o.group === group).map((def) => (
              <OverlayToggle key={def.id} def={def} dash={dash} />
            ))}
          </ul>
        </fieldset>
      ))}
      <div className="space-y-2 border-t border-slate-800 pt-2 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            className="h-4 w-4 accent-sky-500"
            data-testid="toggle-mask"
            checked={dash.showMask}
            onChange={(e) => dash.setShowMask(e.target.checked)}
          />
          Mask unobservable zones (vegetated / decorrelated)
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            className="h-4 w-4 accent-sky-500"
            data-testid="toggle-terrain"
            checked={dash.terrain}
            onChange={(e) => dash.setTerrain(e.target.checked)}
          />
          3D terrain
        </label>
        <label className="flex items-center gap-2">
          <span className="w-28 shrink-0">Overlay opacity</span>
          <input
            type="range"
            min={0.1}
            max={1}
            step={0.05}
            value={dash.overlayOpacity}
            onChange={(e) => dash.setOverlayOpacity(Number(e.target.value))}
            className="min-w-0 flex-1 accent-sky-500"
            aria-label="Overlay opacity"
          />
        </label>
      </div>
    </div>
  );
}

export function EpochTimeline({ dash }: { dash: Dashboard }) {
  const { epochs, epoch, setEpoch, selectedId } = dash;
  if (!selectedId) return <p className="text-sm text-slate-400">Select a facility to load its OPERA epochs.</p>;
  if (!epochs.length) return <p className="text-sm text-slate-400">No time-dependent OPERA products for this facility.</p>;
  const index = epoch === null ? epochs.length - 1 : Math.max(0, epochs.findIndex((e) => e.time === epoch));
  const current = epochs[index];
  const go = (i: number) => {
    const clamped = Math.min(epochs.length - 1, Math.max(0, i));
    setEpoch(clamped === epochs.length - 1 ? null : epochs[clamped].time);
  };
  const products = [...new Set(epochs.flatMap((e) => e.products))];
  return (
    <div className="space-y-2" data-testid="timeline">
      <div className="flex items-center justify-between text-sm">
        <span>
          <span className="font-mono" data-testid="epoch-label">
            {formatDate(current.time)}
          </span>
          <span className="ml-2 text-xs text-slate-400">{current.products.join(' + ')}</span>
        </span>
        <span className="text-xs text-slate-400">
          {index + 1} / {epochs.length}
          {epoch === null && ' (latest)'}
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={epochs.length - 1}
        step={1}
        value={index}
        onChange={(e) => go(Number(e.target.value))}
        className="w-full accent-sky-500"
        aria-label="OPERA epoch"
        aria-valuetext={formatDate(current.time)}
        data-testid="epoch-slider"
      />
      <div className="relative h-3" aria-hidden>
        {epochs.map((e, i) => (
          <span
            key={e.time}
            className={`absolute top-0 h-3 w-px ${e.products.includes('DSWx') ? 'bg-cyan-400' : 'bg-slate-600'}`}
            style={{ left: `${epochs.length > 1 ? (i / (epochs.length - 1)) * 100 : 50}%` }}
          />
        ))}
      </div>
      <div className="flex items-center gap-2 text-xs">
        <button type="button" className="rounded bg-slate-800 px-2 py-1 hover:bg-slate-700" onClick={() => go(index - 1)} disabled={index === 0}>
          ◀ Prev
        </button>
        <button type="button" className="rounded bg-slate-800 px-2 py-1 hover:bg-slate-700" onClick={() => go(index + 1)} disabled={index === epochs.length - 1}>
          Next ▶
        </button>
        <button type="button" className="rounded bg-slate-800 px-2 py-1 hover:bg-slate-700" onClick={() => setEpoch(null)} disabled={epoch === null}>
          Latest
        </button>
        <span className="ml-auto text-slate-400">
          {products.map((p) => (
            <span key={p} className="ml-2 inline-flex items-center gap-1">
              <span className={`inline-block h-2 w-px ${p === 'DSWx' ? 'bg-cyan-400' : 'bg-slate-500'}`} />
              {p}
            </span>
          ))}
        </span>
      </div>
      <p className="text-[11px] text-slate-500">Time-dependent matrices use the nearest available epoch; static matrices are unaffected.</p>
    </div>
  );
}

function fmt(v: number | null | undefined, digits = 3, unit = ''): string {
  return v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v.toFixed(digits)}${unit ? ` ${unit}` : ''}`;
}

export function PixelPanel({ dash }: { dash: Dashboard }) {
  const { pick, selectedId } = dash;
  if (!selectedId) return null;
  if (!pick) return <p className="text-sm text-slate-400">Click inside the facility footprint to inspect a 30 m cell.</p>;
  const d = pick.data;
  return (
    <div data-testid="pixel-panel" className="space-y-2 text-sm">
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs">
          {pick.lat.toFixed(5)}, {pick.lon.toFixed(5)}
        </span>
        <button type="button" className="text-xs text-slate-400 underline" onClick={dash.clearPick}>
          Clear
        </button>
      </div>
      {pick.state === 'loading' && <p className="text-slate-400">Loading cell…</p>}
      {pick.state === 'error' && (
        <p role="alert" className="text-red-300">
          {pick.error}
        </p>
      )}
      {d && (
        <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-xs">
          {d.classification && (
            <>
              <dt className="text-slate-400">Regime</dt>
              <dd data-testid="pixel-regime">
                {d.classification.regime} · {d.classification.regime_meaning}
                {d.classification.low_confidence && <span className="ml-1 text-amber-300">(low confidence)</span>}
              </dd>
              {d.classification.failure_time_estimate && (
                <>
                  <dt className="text-slate-400">Trend change</dt>
                  <dd>
                    {formatDate(d.classification.failure_time_estimate)} [95 %: {formatDate(d.classification.failure_time_lower)} –{' '}
                    {formatDate(d.classification.failure_time_upper)}]
                  </dd>
                </>
              )}
            </>
          )}
          {d.spectral && (
            <>
              <dt className="text-slate-400">SAI</dt>
              <dd>{fmt(d.spectral.sai)}</dd>
              <dt className="text-slate-400">Unmixing RMSE</dt>
              <dd data-testid="pixel-rmse">
                {fmt(d.spectral.rmse, 4)} {d.spectral.rmse !== null && d.spectral.rmse > 0.05 && <span className="text-amber-300">above 0.05</span>}
              </dd>
            </>
          )}
          {d.kinematics && (
            <>
              <dt className="text-slate-400">Coherence</dt>
              <dd data-testid="pixel-coherence">
                {fmt(d.kinematics.temporal_coherence, 2)}{' '}
                {d.kinematics.temporal_coherence !== null && d.kinematics.temporal_coherence < 0.65 && (
                  <span className="text-amber-300">decorrelated</span>
                )}
              </dd>
              <dt className="text-slate-400">Kinematic status</dt>
              <dd>{d.kinematics.status ?? '—'}</dd>
            </>
          )}
          {d.hydrology && (
            <>
              <dt className="text-slate-400">Elevation / slope</dt>
              <dd>
                {fmt(d.hydrology.elevation_m, 0, 'm')} / {fmt(d.hydrology.slope_deg, 1, '°')}
              </dd>
            </>
          )}
          {Object.entries(d.unavailable).map(([k, v]) => (
            <div key={k} className="col-span-2 text-slate-500">
              {k}: {v}
            </div>
          ))}
        </dl>
      )}
      {d?.classification && (
        <p className="text-[11px] text-slate-500">Trend-change dates are statistical extrapolations, not failure forecasts.</p>
      )}
    </div>
  );
}

export default function Sidebar({ dash, className = '' }: SidebarProps) {
  return (
    <aside data-testid="sidebar" aria-label="Dashboard controls" className={`flex flex-col bg-slate-950 text-slate-100 ${className}`}>
      <header className="border-b border-slate-800 px-4 py-3">
        <h1 className="text-base font-semibold tracking-tight">LithoCreep</h1>
        <p className="text-xs text-slate-400">Tailings screening from NASA / OPERA satellite products</p>
      </header>
      <Section title="Active facility" id="sec-facility">
        <FacilityPicker dash={dash} />
      </Section>
      <Section title="Overlay matrices" id="sec-layers">
        <LayerToggles dash={dash} />
      </Section>
      <Section title="OPERA timeline" id="sec-timeline">
        <EpochTimeline dash={dash} />
      </Section>
      <Section title="Selected cell" id="sec-pixel">
        <PixelPanel dash={dash} />
      </Section>
    </aside>
  );
}
