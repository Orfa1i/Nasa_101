/**
 * Dashboard controls: active facility picker, overlay matrix toggles, the OPERA epoch timeline and
 * the Double Inspector (spectroscopy / kinematics / hydrology charts of the picked cell).
 */
import { useEffect, useRef, useState } from 'react';

import dynamic from 'next/dynamic';

import ErrorBoundary from '@/components/ErrorBoundary';

// The chart panels (Recharts) load on first use, keeping them out of the first-load bundle.
const panelLoading = () => <p className="text-sm text-slate-400">Loading panel…</p>;
const Inspector = dynamic(() => import('@/components/Inspector'), { loading: panelLoading });
const ScenarioCtrl = dynamic(() => import('@/components/ScenarioCtrl'), { loading: panelLoading });
const AuditAgent = dynamic(() => import('@/components/AuditAgent'), { loading: panelLoading });
import type { Dashboard } from '@/hooks/useDashboard';
import { ALERT_COLORS, ALERT_LABELS, alertColorName } from '@/utils/facilities';
import { OVERLAYS, type OverlayDef } from '@/utils/overlays';
import { clearIssues, useIssues } from '@/utils/issues';
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
  if (registryState === 'loading' && !facilities.length) return <p className="text-sm text-slate-400">Loading facilities…</p>;
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
      {registryState === 'stale' && (
        <div role="alert" data-testid="registry-stale" className="rounded border border-amber-500/50 bg-amber-950/50 p-2 text-[11px] text-amber-100">
          Backend unreachable ({registryError}): showing the facility registry cached{' '}
          {dash.registryCachedAt ? new Date(dash.registryCachedAt).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : 'earlier'}. Layers and cells need the backend.
          <button type="button" className="ml-2 underline" onClick={() => void dash.reloadRegistry()}>
            Retry
          </button>
        </div>
      )}
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

type SidebarTab = 'layers' | 'inspector' | 'scenarios' | 'audit';
const SIDEBAR_TABS: { id: SidebarTab; label: string }[] = [
  { id: 'layers', label: 'Layers' },
  { id: 'inspector', label: 'Inspector' },
  { id: 'scenarios', label: 'Scenarios' },
  { id: 'audit', label: 'Audit' },
];

export function IssuesIndicator() {
  const issues = useIssues();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', close);
    };
  }, [open]);
  if (!issues.length) return <span data-testid="issues-none" className="text-[11px] text-slate-500">No issues</span>;
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        data-testid="issues-button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="rounded border border-amber-500/60 bg-amber-950/60 px-2 py-0.5 text-[11px] text-amber-100"
      >
        ! {issues.length} issue{issues.length > 1 ? 's' : ''}
      </button>
      {open && (
        <div data-testid="issues-list" className="absolute right-0 z-20 mt-1 max-h-72 w-72 overflow-y-auto rounded-md border border-slate-700 bg-slate-950 p-2 text-[11px] shadow-xl">
          <ul className="space-y-1.5">
            {issues.map((i) => (
              <li key={i.id} data-source={i.source}>
                <span className="font-mono text-slate-500">{new Date(i.at).toISOString().slice(11, 19)}</span>{' '}
                <span className="uppercase text-amber-300">{i.source}</span> {i.message}
                {i.count > 1 && <span className="text-slate-500"> ×{i.count}</span>}
                {i.fallback && <span className="block text-emerald-300">fallback: {i.fallback}</span>}
                {i.detail && <span className="block truncate text-slate-500">{i.detail}</span>}
              </li>
            ))}
          </ul>
          <button type="button" className="mt-2 text-slate-400 underline" onClick={() => { clearIssues(); setOpen(false); }}>
            Clear
          </button>
        </div>
      )}
    </div>
  );
}

export default function Sidebar({ dash, className = '' }: SidebarProps) {
  const [tab, setTab] = useState<SidebarTab>('layers');
  // Panels stay mounted once opened, so scenario and audit results survive switching tabs.
  const [visited, setVisited] = useState<Set<SidebarTab>>(() => new Set(['layers']));
  useEffect(() => setVisited((v) => (v.has(tab) ? v : new Set(v).add(tab))), [tab]);
  const show = (id: SidebarTab) => (tab === id ? '' : 'hidden');
  // A map click picks a cell: show it in the Inspector (unless a scenario or audit panel is open).
  const pickKey = dash.pick ? `${dash.pick.lat},${dash.pick.lon}` : null;
  useEffect(() => {
    if (pickKey) setTab((t) => (t === 'layers' ? 'inspector' : t));
  }, [pickKey]);
  return (
    <aside data-testid="sidebar" aria-label="Dashboard controls" className={`flex flex-col bg-slate-950 text-slate-100 ${className}`}>
      <header className="flex items-start justify-between gap-2 border-b border-slate-800 px-4 py-3">
        <div>
          <h1 className="text-base font-semibold tracking-tight">LithoCreep</h1>
          <p className="text-xs text-slate-400">Tailings screening from NASA / OPERA satellite products</p>
        </div>
        <IssuesIndicator />
      </header>
      <Section title="Active facility" id="sec-facility">
        <FacilityPicker dash={dash} />
      </Section>
      <div role="tablist" aria-label="Dashboard panels" className="sticky top-0 z-10 grid grid-cols-4 border-b border-slate-800 bg-slate-950">
        {SIDEBAR_TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            data-testid={`sidebar-tab-${t.id}`}
            onClick={() => setTab(t.id)}
            className={`border-b-2 px-1 py-2 text-xs ${tab === t.id ? 'border-sky-400 text-slate-100' : 'border-transparent text-slate-400 hover:text-slate-200'}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" aria-label={SIDEBAR_TABS.find((t) => t.id === tab)?.label}>
        <div className={show('layers')}>
          <Section title="Overlay matrices" id="sec-layers">
            <LayerToggles dash={dash} />
          </Section>
          <Section title="OPERA timeline" id="sec-timeline">
            <EpochTimeline dash={dash} />
          </Section>
        </div>
        {visited.has('inspector') && (
          <div className={show('inspector')}>
            <Section title="Double Inspector" id="sec-inspector">
              <ErrorBoundary name="Inspector">
                <Inspector dash={dash} />
              </ErrorBoundary>
            </Section>
          </div>
        )}
        {visited.has('scenarios') && (
          <div className={show('scenarios')}>
            <Section title="Scenarios" id="sec-scenarios">
              <ErrorBoundary name="Scenarios">
                <ScenarioCtrl dash={dash} />
              </ErrorBoundary>
            </Section>
          </div>
        )}
        {visited.has('audit') && (
          <div className={show('audit')}>
            <Section title="Compliance Copilot" id="sec-audit">
              <ErrorBoundary name="Compliance Copilot">
                <AuditAgent dash={dash} />
              </ErrorBoundary>
            </Section>
          </div>
        )}
      </div>
    </aside>
  );
}
