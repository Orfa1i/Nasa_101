/** Heads-up overlays drawn over the map: provenance watermark, data-quality panel and legend. */
import { useState } from 'react';

import type { MatrixSlot } from '@/hooks/useDashboard';
import type { Matrix } from '@/utils/api';
import { cssColor, rampGradient } from '@/utils/colormap';
import {
  MASK_BOTH,
  MASK_DECORRELATED,
  MASK_STYLE,
  MASK_VEGETATED,
  OVERLAY_BY_ID,
  fractionBeyond,
  matrixStats,
  resolveDomain,
  type OverlayId,
} from '@/utils/overlays';
import type { Provenance } from '@/utils/provenance';
import { formatDate } from '@/utils/provenance';

// --------------------------------------------------------------------------- //
// Watermark
// --------------------------------------------------------------------------- //

const BADGE: Record<Provenance['state'], string> = {
  authentic: 'border-emerald-400/70 bg-emerald-950/80 text-emerald-100',
  simulated: 'border-amber-400/80 bg-amber-950/85 text-amber-100',
  none: 'border-slate-500/60 bg-slate-900/80 text-slate-200',
};

/** Faint full-map "SIMULATED" backdrop; must be placed directly in the map container. */
export function SimulatedBackdrop({ prov }: { prov: Provenance }) {
  if (prov.state !== 'simulated') return null;
  return (
    <div aria-hidden data-testid="simulated-backdrop" className="pointer-events-none absolute inset-0 flex items-center justify-center overflow-hidden">
      <span className="-rotate-[18deg] select-none whitespace-nowrap text-5xl font-black tracking-[0.3em] text-amber-300/15 sm:text-7xl lg:text-8xl">
        SIMULATED
      </span>
    </div>
  );
}

export function Watermark({ prov, epoch }: { prov: Provenance; epoch: string | null }) {
  return (
    <>
      <div
        role="status"
        data-testid="watermark"
        data-provenance={prov.state}
        className={`pointer-events-auto w-full min-w-0 max-w-full rounded-md border px-3 py-2 shadow-lg backdrop-blur md:w-auto ${BADGE[prov.state]}`}
      >
        <p className="text-xs font-bold tracking-widest" data-testid="watermark-headline">
          {prov.headline}
        </p>
        {prov.sources.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-[11px] leading-tight opacity-90">
            {prov.sources.map((s) => (
              <li key={`${s.synthetic}|${s.label}`} className="truncate" title={s.label}>
                <span className="font-semibold">{s.synthetic ? 'SIM' : 'OBS'}</span> · {s.products.join(', ')}: {s.label}
              </li>
            ))}
          </ul>
        )}
        {epoch && <p className="mt-1 text-[11px] opacity-80">Epoch: {formatDate(epoch)}</p>}
      </div>
    </>
  );
}

// --------------------------------------------------------------------------- //
// Data quality
// --------------------------------------------------------------------------- //

function pct(v: number | null): string {
  return v === null ? '—' : `${(v * 100).toFixed(1)} %`;
}

function num(v: number | null | undefined, d: number): string {
  return v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(d);
}

export interface HoverReadout {
  lon: number;
  lat: number;
  rmse: number | null;
  coherence: number | null;
  maskCode: number | null;
}

export function QualityPanel({
  rmse,
  coherence,
  mask,
  hover,
}: {
  rmse: MatrixSlot | undefined;
  coherence: MatrixSlot | undefined;
  mask: Matrix | null;
  hover: HoverReadout | null;
}) {
  const r = rmse?.matrix ? matrixStats(rmse.matrix) : null;
  const c = coherence?.matrix ? matrixStats(coherence.matrix) : null;
  const rHigh = rmse?.matrix ? fractionBeyond(rmse.matrix, 0.05, true) : null;
  const cLow = coherence?.matrix ? fractionBeyond(coherence.matrix, 0.65, false) : null;
  const maskCounts = { veg: 0, dec: 0 };
  if (mask) {
    for (const row of mask.values)
      for (const v of row) {
        if (v === MASK_VEGETATED || v === MASK_BOTH) maskCounts.veg++;
        if (v === MASK_DECORRELATED || v === MASK_BOTH) maskCounts.dec++;
      }
  }
  const state = (s: MatrixSlot | undefined) => (s?.state === 'loading' ? 'loading…' : s?.state === 'error' ? 'n/a' : '—');
  return (
    <section
      data-testid="quality-panel"
      aria-label="Data quality"
      className="pointer-events-auto w-full shrink-0 rounded-md border border-slate-600/60 bg-slate-950/85 px-3 py-2 text-[11px] text-slate-200 shadow-lg backdrop-blur md:w-64"
    >
      <h3 className="mb-1 text-[11px] font-bold uppercase tracking-wider text-slate-300">Data quality</h3>
      <table className="w-full">
        <thead>
          <tr className="text-slate-400">
            <th className="text-left font-normal" />
            <th className="text-right font-normal">median</th>
            <th className="text-right font-normal" title="RMSE: 95th percentile; coherence: 5th percentile">tail</th>
            <th className="text-right font-normal">flagged</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          <tr data-testid="quality-rmse">
            <td className="font-sans">Unmixing RMSE</td>
            {r ? (
              <>
                <td className="text-right">{num(r.median, 4)}</td>
                <td className="text-right" title="95th percentile">{num(r.p95, 4)}</td>
                <td className="text-right" title="cells with RMSE > 0.05 (low-confidence unmixing)">
                  {pct(rHigh)}
                </td>
              </>
            ) : (
              <td colSpan={3} className="text-right font-sans text-slate-400">
                {state(rmse)}
              </td>
            )}
          </tr>
          <tr data-testid="quality-coherence">
            <td className="font-sans">Coherence</td>
            {c ? (
              <>
                <td className="text-right">{num(c.median, 2)}</td>
                <td className="text-right" title="5th percentile (the low tail matters for coherence)">
                  {num(c.p05, 2)}
                </td>
                <td className="text-right" title="cells with coherence < 0.65 (decorrelated)">
                  {pct(cLow)}
                </td>
              </>
            ) : (
              <td colSpan={3} className="text-right font-sans text-slate-400">
                {state(coherence)}
              </td>
            )}
          </tr>
        </tbody>
      </table>
      <p className="mt-1 hidden text-slate-400 sm:block">
        Thresholds: RMSE &gt; 0.05, coherence &lt; 0.65.
        {mask && (
          <>
            {' '}
            Masked: {maskCounts.veg} vegetated, {maskCounts.dec} decorrelated cells.
          </>
        )}
      </p>
      {hover && (
        <p data-testid="quality-hover" className="mt-1 border-t border-slate-700 pt-1 font-mono">
          cursor RMSE {num(hover.rmse, 4)} · coh {num(hover.coherence, 2)}
          {hover.maskCode ? <span className="text-amber-300"> · {MASK_STYLE[hover.maskCode]?.label.split(':')[1]?.trim()}</span> : null}
        </p>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Legend
// --------------------------------------------------------------------------- //

export function Legend({
  active,
  matrices,
  showMask,
  hasMask,
}: {
  active: OverlayId[];
  matrices: Partial<Record<OverlayId, MatrixSlot>>;
  showMask: boolean;
  hasMask: boolean;
}) {
  // Collapsed by default on phones, where an open legend would cover most of the map.
  const [open, setOpen] = useState(() => typeof window === 'undefined' || !window.matchMedia || window.matchMedia('(min-width: 640px)').matches);
  const shown = active.filter((id) => matrices[id]?.matrix);
  if (!shown.length) return null;
  return (
    <section
      data-testid="legend"
      aria-label="Legend"
      className="pointer-events-auto flex min-h-0 max-h-full w-full flex-col rounded-md border border-slate-600/60 bg-slate-950/85 text-[11px] text-slate-200 shadow-lg backdrop-blur sm:w-72"
    >
      <button
        type="button"
        data-testid="legend-toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-1.5 text-left font-bold uppercase tracking-wider text-slate-300"
      >
        Legend <span aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="min-h-0 overflow-y-auto px-3 pb-2">
          {shown.map((id) => {
            const def = OVERLAY_BY_ID[id];
            const m = matrices[id]!.matrix!;
            return (
              <div key={id} className="mb-2 last:mb-0" data-testid={`legend-${id}`}>
                <p className="font-semibold">
                  {def.label} <span className="font-normal text-slate-400">({m.units})</span>
                </p>
                {def.scale.kind === 'categorical' ? (
                  <ul className="mt-0.5 grid grid-cols-1 gap-0.5">
                    {def.scale.classes.map((c) => (
                      <li key={c.value} className="flex items-center gap-1.5">
                        <span className="inline-block h-2.5 w-3.5 rounded-sm" style={{ backgroundColor: cssColor(c.color) }} />
                        {c.label}
                      </li>
                    ))}
                  </ul>
                ) : (
                  (() => {
                    const scale = def.scale;
                    const { domain, transform } = resolveDomain(scale, m);
                    const label = (v: number) => (transform === 'log10' ? `${Math.round(10 ** v)}` : v.toFixed(def.decimals > 2 ? 2 : def.decimals));
                    const thr = scale.threshold;
                    const thrPos = thr ? ((thr.value - domain[0]) / (domain[1] - domain[0])) * 100 : null;
                    return (
                      <div className="mt-0.5">
                        <div className="relative h-2.5 rounded-sm" style={{ background: rampGradient(scale.ramp) }}>
                          {thrPos !== null && thrPos >= 0 && thrPos <= 100 && (
                            <span className="absolute -top-0.5 h-3.5 w-0.5 bg-white" style={{ left: `${thrPos}%` }} title={thr!.label} />
                          )}
                        </div>
                        <div className="flex justify-between text-slate-400">
                          <span>{label(domain[0])}</span>
                          {thr && <span>{thr.label}</span>}
                          <span>{label(domain[1])}</span>
                        </div>
                        {m.time && <p className="text-slate-400">epoch {formatDate(m.time)}</p>}
                      </div>
                    );
                  })()
                )}
              </div>
            );
          })}
          {showMask && hasMask && (
            <div className="mt-2 border-t border-slate-700 pt-1" data-testid="legend-mask">
              {[MASK_VEGETATED, MASK_DECORRELATED].map((code) => (
                <p key={code} className="flex items-center gap-1.5">
                  <span
                    className="inline-block h-2.5 w-3.5 rounded-sm"
                    style={{
                      background: `repeating-linear-gradient(45deg, ${cssColor(MASK_STYLE[code].color)} 0 2px, transparent 2px 5px)`,
                    }}
                  />
                  {MASK_STYLE[code].label}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
