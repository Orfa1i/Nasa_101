/**
 * Compliance Copilot: runs the Phase 7 audit (POST /api/facilities/{id}/audit), shows its status,
 * and renders the validated GISTM screening brief. GISTM (2020) was co-convened by ICMM, UNEP and
 * PRI; the brief is a screening aid aligned to its principles, not a compliance determination.
 *
 * "Export PDF / Print" prints a dedicated, light-themed copy of the report (rendered into a
 * print-only portal), so the browser's print dialog can save it as PDF in one click.
 */
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import type { Dashboard } from '@/hooks/useDashboard';
import { api, ApiError, type AuditResponse, type AuditStatus, type BriefSection } from '@/utils/api';

type RunState =
  | { kind: 'idle' }
  | { kind: 'running'; startedAt: number }
  | { kind: 'done'; startedAt: number; finishedAt: number; result: AuditResponse }
  | { kind: 'failed'; startedAt: number; finishedAt: number; status: number; message: string; discrepancies: string[] };

const RISK_STYLE: Record<string, string> = {
  'Tier-3 / Active Instability': 'border-red-400 bg-red-950 text-red-100',
  'Tier-2 / Alert': 'border-orange-400 bg-orange-950 text-orange-100',
  'Tier-1 / Legacy Seepage Watch': 'border-yellow-400 bg-yellow-950 text-yellow-100',
  'No Alert / Benign Consolidation': 'border-emerald-400 bg-emerald-950 text-emerald-100',
  'Indeterminate / Insufficient Data': 'border-slate-400 bg-slate-900 text-slate-200',
};

function Section({ title, section, print }: { title: string; section: BriefSection; print: boolean }) {
  return (
    <section className="break-inside-avoid">
      <h4 className={`text-xs font-semibold ${print ? 'text-black' : 'text-slate-200'}`}>{title}</h4>
      <p className={`mt-0.5 text-[11px] leading-snug ${print ? 'text-gray-800' : 'text-slate-300'}`}>{section.summary}</p>
      {section.facts.length > 0 && (
        <table className="mt-1 w-full text-[10px]">
          <tbody>
            {section.facts.map((f) => (
              <tr key={`${f.tool}:${f.path}`} className={print ? 'border-t border-gray-300' : 'border-t border-slate-800'}>
                <td className={print ? 'text-gray-700' : 'text-slate-400'}>{f.label}</td>
                <td className="whitespace-nowrap pl-2 text-right font-mono">
                  {f.value} {f.unit}
                </td>
                <td className={`pl-2 font-mono ${print ? 'text-gray-500' : 'text-slate-500'}`} title={`${f.tool}:${f.path}`}>
                  {f.tool}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function AuditReport({ result, finishedAt, print = false }: { result: AuditResponse; finishedAt: number; print?: boolean }) {
  const b = result.brief;
  const muted = print ? 'text-gray-600' : 'text-slate-400';
  return (
    <article data-testid={print ? 'audit-report-print' : 'audit-report'} className={`space-y-3 ${print ? 'bg-white p-8 text-black' : 'text-slate-200'}`}>
      <header className="space-y-1">
        <p className={`text-[10px] uppercase tracking-widest ${muted}`}>LithoCreep · GISTM-aligned screening brief (GISTM 2020: ICMM, UNEP, PRI)</p>
        <h3 className={`text-sm font-bold ${print ? 'text-black' : 'text-slate-100'}`}>{b.facility}</h3>
        <div className="flex flex-wrap items-center gap-2">
          <span data-testid="audit-risk" className={`rounded border px-2 py-0.5 text-[11px] font-bold ${print ? 'border-black' : RISK_STYLE[b.risk_classification] ?? 'border-slate-500 bg-slate-900 text-slate-200'}`}>
            {b.risk_classification}
          </span>
          <span className={`text-[11px] ${muted}`}>hazard {b.hazard_level}</span>
        </div>
        <p className={`text-[10px] ${muted}`}>
          Generated {new Date(finishedAt).toISOString().replace('T', ' ').slice(0, 19)} UTC by {result.generated_by} · validation{' '}
          <span data-testid="audit-validation">{result.validation.passed ? 'passed' : 'FAILED'}</span> ({result.validation.facts_checked ?? 0} facts,{' '}
          {result.validation.prose_numbers_checked ?? 0} numbers, {result.validation.prediction_windows_checked ?? 0} windows checked against the raw tool outputs)
        </p>
      </header>
      <p className="text-xs leading-snug">{b.executive_summary}</p>
      <Section title="Spectral evidence" section={b.spectral_evidence} print={print} />
      <Section title="Kinematic evidence" section={b.kinematic_evidence} print={print} />
      {b.kinematic_evidence.prediction_windows.length > 0 && (
        <table className="w-full text-[10px]" data-testid="audit-windows">
          <caption className={`text-left text-[11px] font-semibold ${print ? 'text-black' : 'text-slate-200'}`}>
            Estimated trend-change windows (95 %; estimates, not failure dates)
          </caption>
          <tbody>
            {b.kinematic_evidence.prediction_windows.map((w) => (
              <tr key={w.path} className={print ? 'border-t border-gray-300' : 'border-t border-slate-800'}>
                <td>{w.label}</td>
                <td className="whitespace-nowrap pl-2 text-right font-mono">
                  {w.estimate} [{w.lower_95} – {w.upper_95}]
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Section title="Hydrological evidence" section={b.hydrological_evidence} print={print} />
      <section className="break-inside-avoid">
        <h4 className={`text-xs font-semibold ${print ? 'text-black' : 'text-slate-200'}`}>Compliance issues (GISTM principles, paraphrased)</h4>
        <ol className="mt-1 list-decimal space-y-1 pl-4 text-[11px]" data-testid="audit-issues">
          {b.compliance_issues.map((i) => (
            <li key={i.issue}>
              <span className="font-semibold">{i.gistm_principle}.</span> {i.issue}
              <span className={`block font-mono text-[10px] ${muted}`}>{i.evidence_paths.join(' · ')}</span>
            </li>
          ))}
        </ol>
      </section>
      <section className="break-inside-avoid">
        <h4 className={`text-xs font-semibold ${print ? 'text-black' : 'text-slate-200'}`}>Recommendations</h4>
        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px]">
          {b.recommendations.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </section>
      <section className="break-inside-avoid">
        <h4 className={`text-xs font-semibold ${print ? 'text-black' : 'text-slate-200'}`}>Data limitations</h4>
        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px]">
          {b.data_limitations.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </section>
      <p className={`border-t pt-2 text-[10px] leading-snug ${print ? 'border-gray-400 text-gray-700' : 'border-slate-800 text-slate-400'}`} data-testid="audit-disclaimer">
        {b.disclaimer} Screening aid for a qualified reviewer, not a GISTM compliance determination.
      </p>
      <details className="text-[10px]" open={print}>
        <summary className={muted}>Tool calls ({result.tool_calls.length})</summary>
        <ul className="mt-1 font-mono">
          {result.tool_calls.map((c, i) => (
            <li key={i}>
              {c.ok ? '✓' : '✕'} {c.tool}({JSON.stringify(c.input)}){c.error ? ` - ${c.error}` : ''}
            </li>
          ))}
        </ul>
      </details>
    </article>
  );
}

export default function AuditAgent({ dash }: { dash: Dashboard }) {
  const { selected } = dash;
  const [status, setStatus] = useState<AuditStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [mode, setMode] = useState('auto');
  const [query, setQuery] = useState('');
  const [run, setRun] = useState<RunState>({ kind: 'idle' });
  const [now, setNow] = useState(Date.now());
  const [mounted, setMounted] = useState(false);
  const ctrl = useRef<AbortController | null>(null);

  useEffect(() => setMounted(true), []);
  useEffect(() => {
    const c = new AbortController();
    api
      .auditStatus(c.signal)
      .then(setStatus)
      .catch((err) => {
        if (!(err instanceof Error && err.name === 'AbortError')) setStatusError(err instanceof Error ? err.message : String(err));
      });
    return () => c.abort();
  }, []);
  useEffect(() => {
    ctrl.current?.abort();
    setRun({ kind: 'idle' });
  }, [selected?.id]);
  useEffect(() => {
    if (run.kind !== 'running') return;
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [run.kind]);

  if (!selected) return <p className="text-sm text-slate-400">Select a facility to audit.</p>;

  const start = () => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    const startedAt = Date.now();
    setNow(startedAt);
    setRun({ kind: 'running', startedAt });
    api
      .audit(selected.id, { mode, query: query.trim() || null }, c.signal)
      .then((result) => setRun({ kind: 'done', startedAt, finishedAt: Date.now(), result }))
      .catch((err) => {
        if (err instanceof Error && err.name === 'AbortError') return;
        const body = err instanceof ApiError ? (err.body as { discrepancies?: string[] } | undefined) : undefined;
        setRun({
          kind: 'failed',
          startedAt,
          finishedAt: Date.now(),
          status: err instanceof ApiError ? err.status : 0,
          message: err instanceof ApiError ? err.detail : String(err),
          discrepancies: Array.isArray(body?.discrepancies) ? body!.discrepancies : [],
        });
      });
  };

  const download = (result: AuditResponse) => {
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${result.facility_id}_gistm_screening_brief.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const elapsed = (a: number, b: number) => `${((b - a) / 1000).toFixed(1)} s`;
  return (
    <div className="space-y-2" data-testid="audit-agent">
      <p className="text-[11px] leading-snug text-slate-400">
        Compiles a GISTM-aligned screening brief for <span className="text-slate-200">{selected.name}</span> from the local inspection tools, then checks every number in
        it against the raw tool outputs before it is shown.
      </p>
      <p className="text-[11px]" data-testid="audit-copilot-status">
        {status ? (
          status.llm_available ? (
            <span className="text-emerald-300">Claude copilot available ({status.model}).</span>
          ) : (
            <span className="text-slate-300">Claude copilot not configured (no API credentials): briefs use the deterministic template.</span>
          )
        ) : statusError ? (
          <span className="text-red-300">Copilot status unavailable: {statusError}</span>
        ) : (
          'Checking copilot status…'
        )}
      </p>
      <div className="flex items-end gap-2">
        <label className="text-[11px] text-slate-400">
          Mode
          <select value={mode} onChange={(e) => setMode(e.target.value)} data-testid="audit-mode" className="mt-0.5 block rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-xs text-slate-100">
            <option value="auto">auto</option>
            <option value="llm" disabled={!status?.llm_available}>
              Claude copilot
            </option>
            <option value="deterministic">deterministic template</option>
          </select>
        </label>
        <button
          type="button"
          onClick={start}
          disabled={run.kind === 'running'}
          data-testid="audit-run"
          className="rounded bg-sky-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-60"
        >
          {run.kind === 'running' ? 'Auditing…' : 'Run audit'}
        </button>
      </div>
      <label className="block text-[11px] text-slate-400">
        Request (optional)
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          rows={2}
          maxLength={2000}
          placeholder={`Generate regulatory GISTM inspection brief for ${selected.name}`}
          className="mt-0.5 block w-full rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-xs text-slate-100"
        />
      </label>

      <div data-testid="audit-run-status" data-state={run.kind} className="text-[11px]">
        {run.kind === 'idle' && <span className="text-slate-400">No audit run yet.</span>}
        {run.kind === 'running' && <span className="text-sky-300">Running… {elapsed(run.startedAt, now)}</span>}
        {run.kind === 'done' && (
          <span className="text-emerald-300">
            Completed in {elapsed(run.startedAt, run.finishedAt)} · {run.result.generated_by} · {run.result.tool_calls.length} tool call(s) · validation{' '}
            {run.result.validation.passed ? 'passed' : 'failed'}
          </span>
        )}
        {run.kind === 'failed' && (
          <div role="alert" className="rounded border border-red-500/50 bg-red-950/50 p-2 text-red-100">
            Audit failed ({run.status === 0 ? 'backend unreachable' : `HTTP ${run.status}`}): {run.message}
            {run.discrepancies.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-[10px]">
                {run.discrepancies.map((d) => (
                  <li key={d}>{d}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {run.kind === 'done' && (
        <>
          <div className="flex gap-2">
            <button type="button" onClick={() => window.print()} data-testid="audit-print" className="rounded bg-slate-700 px-2.5 py-1 text-xs text-white hover:bg-slate-600">
              Export PDF / Print
            </button>
            <button type="button" onClick={() => download(run.result)} data-testid="audit-download" className="rounded bg-slate-800 px-2.5 py-1 text-xs text-slate-100 hover:bg-slate-700">
              Download JSON
            </button>
          </div>
          <div className="rounded-md border border-slate-800 bg-slate-900 p-2">
            <AuditReport result={run.result} finishedAt={run.finishedAt} />
          </div>
          {mounted &&
            createPortal(
              <div id="audit-print-root" className="hidden print:block">
                <AuditReport result={run.result} finishedAt={run.finishedAt} print />
              </div>,
              document.body,
            )}
        </>
      )}
    </div>
  );
}
