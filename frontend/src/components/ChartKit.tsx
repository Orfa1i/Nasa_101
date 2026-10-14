/** Shared chart furniture for the inspector and scenario panels (Recharts, dataviz palette). */
import { useEffect, useRef, useState, type ReactNode } from 'react';

import { STATUS, VIZ, type StatusLevel } from '@/utils/inspector';


export const AXIS = { stroke: VIZ.grid, tick: { fill: VIZ.axis, fontSize: 10 }, tickLine: false } as const;
export const TOOLTIP = {
  contentStyle: { background: '#020617', border: '1px solid #334155', borderRadius: 6, fontSize: 11, color: VIZ.text },
  labelStyle: { color: VIZ.axis },
  cursor: { stroke: VIZ.axis, strokeDasharray: '3 3' },
} as const;
export const LEGEND = { wrapperStyle: { fontSize: 11, color: VIZ.text }, iconSize: 10 } as const;
export const NO_ANIMATION = { isAnimationActive: false } as const;

export const fmtMonth = (t: number) => new Date(t).toISOString().slice(0, 7);
export const fmtDay = (t: number) => new Date(t).toISOString().slice(0, 10);
export const num = (v: number | null | undefined, d: number) => (v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(d));

/** Measures its width (charts get explicit pixel sizes; falls back to 320 px before layout). */
export function ChartFrame({ title, testId, height, caption, children }: { title: string; testId: string; height: number; caption?: ReactNode; children: (width: number) => ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(320);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => el.clientWidth > 0 && setWidth(el.clientWidth);
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <figure data-testid={testId} className="rounded-md border border-slate-800 bg-slate-900 p-2">
      <figcaption className="mb-1 text-xs font-semibold text-slate-200">{title}</figcaption>
      <div ref={ref} style={{ height }} className="w-full">
        {children(width)}
      </div>
      {caption && <p className="mt-1 text-[11px] leading-snug text-slate-400">{caption}</p>}
    </figure>
  );
}

export function StatusBadge({ level, label, testId }: { level: StatusLevel; label?: string; testId?: string }) {
  const s = STATUS[level];
  return (
    <span data-testid={testId} data-level={level} className="inline-flex items-center gap-1 whitespace-nowrap text-[11px] text-slate-200">
      <span aria-hidden className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full text-[9px] font-bold text-slate-950" style={{ background: s.color }}>
        {s.icon}
      </span>
      {label ?? s.label}
    </span>
  );
}

export function Empty({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <p data-testid={testId} className="rounded-md border border-dashed border-slate-700 px-3 py-4 text-center text-xs text-slate-400">
      {children}
    </p>
  );
}

export function StatTile({ label, value, sub, testId }: { label: string; value: ReactNode; sub?: ReactNode; testId?: string }) {
  return (
    <div data-testid={testId} className="rounded-md border border-slate-800 bg-slate-900 px-2 py-1.5">
      <p className="text-[11px] text-slate-400">{label}</p>
      <p className="font-mono text-base leading-tight text-slate-100">{value}</p>
      {sub && <div className="mt-0.5">{sub}</div>}
    </div>
  );
}

