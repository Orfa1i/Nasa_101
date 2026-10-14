/**
 * Phase 10: scenario panel (backtest + stress test), Compliance Copilot, and the outage fallbacks
 * (cached registry, health banner, Mapbox -> MapLibre, terrain providers, crashed panels).
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect, type ReactNode } from 'react';

import AuditAgent from '@/components/AuditAgent';
import ErrorBoundary from '@/components/ErrorBoundary';
import ScenarioCtrl from '@/components/ScenarioCtrl';
import Sidebar from '@/components/Sidebar';
import { useDashboard, type Dashboard } from '@/hooks/useDashboard';
import { getIssues } from '@/utils/issues';
import { terrainSources } from '@/utils/mapLayers';
import { inArizona } from '@/utils/scenarios';

import { AUDIT, BRUMADINHO, FACILITIES, backtest, requestedUrls, stubFetch, type StubOptions } from './fixtures';

afterEach(() => vi.unstubAllGlobals());

let lastDash: Dashboard | null = null;

function Harness({ facility, pick, children }: { facility?: string; pick?: [number, number]; children: (d: Dashboard) => ReactNode }) {
  const dash = useDashboard();
  lastDash = dash;
  const { registryState, selectFacility, selected, pickPixel } = dash;
  useEffect(() => {
    if (registryState === 'ready' && facility) selectFacility(facility);
  }, [registryState, selectFacility, facility]);
  useEffect(() => {
    if (selected && pick) pickPixel(pick[0], pick[1]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);
  return <>{children(dash)}</>;
}

const urls = (fetch: ReturnType<typeof stubFetch>, part: string) => requestedUrls(fetch).filter((u) => u.includes(part));

// --------------------------------------------------------------------------- //
// Backtest
// --------------------------------------------------------------------------- //

describe('Brumadinho backtest', () => {
  async function open(opts: StubOptions = {}) {
    const fetch = stubFetch(opts);
    render(<Harness>{(d) => <ScenarioCtrl dash={d} />}</Harness>);
    return fetch;
  }

  it('badges a published series as the historical dataset and shows the 40-day inverse-velocity window', async () => {
    await open();
    const badge = await screen.findByTestId('backtest-badge');
    expect(badge).toHaveAttribute('data-kind', 'historical');
    expect(badge).toHaveTextContent('HISTORICAL DATASET · 2019');
    expect(screen.getByText(/collapsed 2019-01-25/)).toBeInTheDocument();
    expect(screen.getByTestId('backtest-source')).toHaveTextContent('Example et al. (published series)');
    const chart = screen.getByTestId('chart-backtest-lambda');
    expect(chart).toHaveTextContent('40-day pre-collapse inverse velocity Λ(t) at toe');
    expect(chart).toHaveTextContent('not a guaranteed failure date');
    expect(chart.querySelectorAll('.recharts-reference-line')).toHaveLength(2); // Λ = 0 and the collapse
    // only epochs inside the window are plotted (window_start 2018-12-16 drops the 2018-12-12 epoch)
    expect(chart.querySelectorAll('.recharts-scatter-symbol')).toHaveLength(6);
    expect(screen.getByTestId('backtest-estimate')).toHaveTextContent('2019-01-26');
    expect(screen.getByTestId('backtest-error')).toHaveTextContent('+1.6 d');
    expect(screen.getByTestId('backtest-within').querySelector('[data-level]')).toHaveAttribute('data-level', 'good');
    expect(within(screen.getByTestId('backtest-sweep')).getAllByRole('row')).toHaveLength(3);
  });

  it('marks a synthetic stand-in as such and never as historical', async () => {
    await open({ backtest: backtest({ historical: false, dataset_label: 'SYNTHETIC STAND-IN: not the published Brumadinho series', citation: 'SYNTHETIC stand-in' }) });
    const badge = await screen.findByTestId('backtest-badge');
    expect(badge).toHaveAttribute('data-kind', 'synthetic');
    expect(badge).toHaveTextContent('SYNTHETIC STAND-IN');
    expect(screen.queryByText('HISTORICAL DATASET · 2019')).not.toBeInTheDocument();
  });

  it('explains what to supply when the CSV is missing, without inventing data', async () => {
    await open({ backtest: 'missing' });
    const box = await screen.findByTestId('backtest-missing');
    expect(box).toHaveTextContent('Historical dataset not supplied');
    expect(box).toHaveTextContent('DATA_DIR/brumadinho_historic.csv');
    expect(box).toHaveTextContent('date, displacement_mm, point_id');
    expect(box).toHaveTextContent('does not reconstruct or invent');
    expect(screen.queryByTestId('chart-backtest-lambda')).not.toBeInTheDocument();
  });

  it('re-issues the forecast for an earlier cut-off and another point', async () => {
    const fetch = await open();
    await screen.findByTestId('chart-backtest-lambda');
    fireEvent.change(screen.getByTestId('backtest-cutoff'), { target: { value: '3' } });
    await waitFor(() => expect(urls(fetch, 'cutoff_days=3')).toHaveLength(1));
    expect(screen.getByTestId('backtest-cutoff-label')).toHaveTextContent('3');
    await userEvent.selectOptions(screen.getByTestId('backtest-point'), 'crest');
    await waitFor(() => expect(urls(fetch, 'point=crest')).toHaveLength(1));
  });

  it('reports a failed backtest request', async () => {
    await open({ backtest: { status: 422, detail: 'brumadinho_historic.csv is malformed: missing columns' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Backtest unavailable: 422: brumadinho_historic.csv is malformed');
  });
});

// --------------------------------------------------------------------------- //
// Stress test
// --------------------------------------------------------------------------- //

describe('stress-test simulator', () => {
  async function openStress(opts: StubOptions & { facility?: string; pick?: [number, number] } = {}) {
    const fetch = stubFetch({ extraFacilities: [BRUMADINHO], ...opts });
    render(<Harness facility={opts.facility ?? 'sierrita'} pick={opts.pick}>{(d) => <ScenarioCtrl dash={d} />}</Harness>);
    await userEvent.click(await screen.findByTestId('scenario-tab-stress'));
    return fetch;
  }

  it('limits stress tests to Arizona facilities', async () => {
    expect(inArizona(FACILITIES[0])).toBe(true);
    expect(inArizona(BRUMADINHO)).toBe(false);
    await openStress({ facility: 'brumadinho' });
    expect(await screen.findByTestId('stress-not-az')).toHaveTextContent('limited to Arizona (AZ) facilities');
    expect(screen.getByTestId('stress-warning')).toHaveTextContent('SIMULATED HAZARD SCENARIO');
  });

  it('asks for a cell, then runs the scenario with the picked coordinates and slider intensity', async () => {
    const fetch = await openStress();
    expect(await screen.findByTestId('stress-needs-cell')).toBeInTheDocument();
    expect(urls(fetch, 'stress-test')).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: 'Use the facility centre' }));
    await waitFor(() => expect(urls(fetch, 'stress-test')).toHaveLength(1));
    expect(urls(fetch, 'stress-test')[0]).toBe('/api/facilities/sierrita/stress-test?lat=31.865&lon=-111.08&intensity=0.5');
    await waitFor(() => expect(screen.getByTestId('stress-status')).toHaveAttribute('data-state', 'ready'));

    // slider to 100 %: one debounced request with intensity=1
    fireEvent.change(screen.getByTestId('stress-slider'), { target: { value: '1' } });
    await waitFor(() => expect(urls(fetch, 'intensity=1')).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId('stress-regime')).toHaveTextContent('Regime 4: active instability'));
    expect(screen.getByTestId('stress-intensity-label')).toHaveTextContent('100 %');
    expect(screen.getByTestId('stress-sai')).toHaveTextContent('0.430');
    expect(screen.getByTestId('stress-status-kin')).toHaveTextContent('accelerating-forecast');
    expect(screen.getByTestId('stress-compare')).toHaveTextContent('SIMULATED');
    expect(screen.getByTestId('chart-stress-displacement')).toHaveTextContent('SIMULATED');
    expect(screen.getByTestId('chart-stress-setback')).toHaveTextContent('15 m proximity alert');
  });

  it('publishes the scenario regimes as a labelled test overlay, and withdraws it', async () => {
    await openStress({ pick: [-111.1, 31.86] });
    await waitFor(() => expect(lastDash?.stressOverlay).not.toBeNull());
    expect(lastDash!.stressOverlay!.matrix.source.synthetic_or_mock).toBe(true);
    expect(lastDash!.stressOverlay!.intensity).toBe(0.5);
    await userEvent.click(screen.getByTestId('stress-overlay-toggle'));
    await waitFor(() => expect(lastDash?.stressOverlay).toBeNull());
  });

  it('reports a failed stress test', async () => {
    await openStress({ pick: [-111.1, 31.86], stress: { status: 422, detail: 'point lies outside the kinematics grid' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Stress test failed: 422: point lies outside the kinematics grid');
  });
});

// --------------------------------------------------------------------------- //
// Compliance Copilot
// --------------------------------------------------------------------------- //

describe('Compliance Copilot', () => {
  async function openAudit(opts: StubOptions = {}) {
    const fetch = stubFetch(opts);
    render(<Harness facility="sierrita">{(d) => <AuditAgent dash={d} />}</Harness>);
    await screen.findByTestId('audit-agent');
    return fetch;
  }

  it('shows the copilot status and runs an audit with the chosen mode', async () => {
    const fetch = await openAudit();
    await waitFor(() => expect(screen.getByTestId('audit-copilot-status')).toHaveTextContent('Claude copilot not configured'));
    expect(screen.getByRole('option', { name: 'Claude copilot' })).toBeDisabled();
    expect(screen.getByTestId('audit-run-status')).toHaveAttribute('data-state', 'idle');
    await userEvent.selectOptions(screen.getByTestId('audit-mode'), 'deterministic');
    await userEvent.type(screen.getByRole('textbox'), 'Generate regulatory GISTM inspection brief for Sierrita Complex');
    await userEvent.click(screen.getByTestId('audit-run'));
    await waitFor(() => expect(screen.getByTestId('audit-run-status')).toHaveAttribute('data-state', 'done'));
    const call = fetch.mock.calls.find(([u]) => String(u).endsWith('/audit'))!;
    expect((call[1] as RequestInit).method).toBe('POST');
    expect(JSON.parse(String((call[1] as RequestInit).body))).toEqual({ mode: 'deterministic', query: 'Generate regulatory GISTM inspection brief for Sierrita Complex' });
    expect(screen.getByTestId('audit-run-status')).toHaveTextContent('deterministic-template · 1 tool call(s) · validation passed');
  });

  it('renders the GISTM-aligned report and exports it to print / PDF and JSON', async () => {
    await openAudit();
    await userEvent.click(screen.getByTestId('audit-run'));
    const report = await screen.findByTestId('audit-report');
    expect(within(report).getByTestId('audit-risk')).toHaveTextContent('Tier-3 / Active Instability');
    expect(report).toHaveTextContent('GISTM 2020: ICMM, UNEP, PRI');
    expect(report).toHaveTextContent('Pixels with SAI above threshold (confident)82 pixels');
    expect(within(report).getByTestId('audit-windows')).toHaveTextContent('2025-01-08 [2024-12-29 – 2025-01-19]');
    expect(within(report).getByTestId('audit-issues')).toHaveTextContent('Principle 7');
    expect(within(report).getByTestId('audit-disclaimer')).toHaveTextContent('not a GISTM compliance determination');

    // the print-only copy lives outside the app root, hidden on screen and shown in print
    const printCopy = document.getElementById('audit-print-root')!;
    expect(printCopy).toHaveClass('hidden', 'print:block');
    expect(printCopy.parentElement).toBe(document.body);
    expect(within(printCopy).getByTestId('audit-report-print')).toHaveTextContent('Freeport-McMoRan Sierrita TSF');

    const print = vi.spyOn(window, 'print').mockImplementation(() => {});
    await userEvent.click(screen.getByTestId('audit-print'));
    expect(print).toHaveBeenCalledTimes(1);
    const create = vi.fn(() => 'blob:x');
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    await userEvent.click(screen.getByTestId('audit-download'));
    expect(create).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('shows validation discrepancies when the copilot brief is rejected', async () => {
    await openAudit({ audit: { status: 502, detail: { message: 'Brief failed validation', discrepancies: ['spectral_evidence.facts[0]: 83 != raw 82'] } } });
    await userEvent.click(screen.getByTestId('audit-run'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Audit failed (HTTP 502): Brief failed validation');
    expect(alert).toHaveTextContent('83 != raw 82');
    expect(screen.queryByTestId('audit-report')).not.toBeInTheDocument();
  });

  it('reports an unreachable backend', async () => {
    const fetch = stubFetch();
    render(<Harness facility="sierrita">{(d) => <AuditAgent dash={d} />}</Harness>);
    await screen.findByTestId('audit-agent');
    fetch.mockImplementation(async () => {
      throw new TypeError('Failed to fetch');
    });
    await userEvent.click(screen.getByTestId('audit-run'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Audit failed (backend unreachable)');
    expect(getIssues().some((i) => i.source === 'api' && i.message.includes('backend unreachable'))).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// Fallbacks and error capture
// --------------------------------------------------------------------------- //

describe('outage fallbacks', () => {
  it('falls back to the cached registry when the API is down, and says so', async () => {
    stubFetch();
    const first = render(<Harness>{(d) => <Sidebar dash={d} />}</Harness>);
    await waitFor(() => expect(lastDash?.registryState).toBe('ready'));
    first.unmount();

    stubFetch({ facilitiesStatus: 503 });
    render(<Harness>{(d) => <Sidebar dash={d} />}</Harness>);
    const notice = await screen.findByTestId('registry-stale');
    expect(notice).toHaveTextContent('Backend unreachable');
    expect(notice).toHaveTextContent('cached');
    expect(within(screen.getByRole('list', { name: 'Facilities by alert status' })).getAllByRole('button')).toHaveLength(2);
    expect(lastDash?.registryState).toBe('stale');
    // the outage and the fallback are both in the issues log, surfaced in the sidebar header
    await userEvent.click(screen.getByTestId('issues-button'));
    const log = screen.getByTestId('issues-list');
    expect(log).toHaveTextContent('503 database offline');
    expect(log).toHaveTextContent('fallback: cached copy from');
  });

  it('keeps working without localStorage', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    stubFetch({ facilitiesStatus: 503 });
    render(<Harness>{(d) => <Sidebar dash={d} />}</Harness>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Backend unavailable');
    getItem.mockRestore();
    setItem.mockRestore();
  });

  it('contains a crashed panel and reports it', async () => {
    function Boom(): JSX.Element {
      throw new Error('kaboom');
    }
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <ErrorBoundary name="Inspector">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('crashed-inspector')).toHaveTextContent('Inspector stopped working.');
    expect(screen.getByTestId('crashed-inspector')).toHaveTextContent('kaboom');
    expect(getIssues()[0]).toMatchObject({ source: 'render', message: 'Inspector crashed: kaboom' });
    quiet.mockRestore();
  });

  it('orders terrain providers Mapbox first, then the open tiles', () => {
    expect(terrainSources('pk.test').map((s) => s.provider)).toEqual(['mapbox', 'open']);
    expect(terrainSources(undefined).map((s) => s.provider)).toEqual(['open']);
  });
});
