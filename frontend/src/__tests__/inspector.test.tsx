/**
 * Double Inspector: chart data preparation, and the rendered panels (real Recharts SVG in jsdom)
 * driven by the real useDashboard hook against the API fixtures.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect } from 'react';

import Inspector from '@/components/Inspector';
import { useDashboard } from '@/hooks/useDashboard';
import type { DashboardSummary, PixelResponse } from '@/utils/api';
import {
  abundanceGroups,
  coherenceCheck,
  fukuzonoRows,
  kinematicRows,
  ms,
  setbackChecks,
  setbackLevel,
  spectrumRows,
} from '@/utils/inspector';

import { ACTIVE, DASHBOARD, FIT, KINEMATICS, PIXEL, SPECTRUM, requestedUrls, stubFetch, type StubOptions } from './fixtures';

afterEach(() => vi.unstubAllGlobals());

// --------------------------------------------------------------------------- //
// Data preparation
// --------------------------------------------------------------------------- //

describe('chart data', () => {
  it('keeps all 285 bands and breaks the measured and fitted lines at the water-vapour channels', () => {
    const rows = spectrumRows(SPECTRUM);
    const inactive = ACTIVE.filter((a) => !a).length;
    expect(rows).toHaveLength(285);
    expect(inactive).toBeGreaterThan(30);
    expect(rows.filter((r) => r.measured === null)).toHaveLength(inactive);
    expect(rows.filter((r) => r.modelled === null)).toHaveLength(inactive);
    rows.forEach((r) => {
      const inWindow = (r.wl >= 1340 && r.wl <= 1460) || (r.wl >= 1790 && r.wl <= 1970);
      expect(r.measured === null).toBe(inWindow);
    });
  });

  it('groups endmember fractions into jarosite, smectite, goethite, other and background', () => {
    const g = Object.fromEntries(abundanceGroups(SPECTRUM.abundance).map((x) => [x.key, x.value]));
    expect(g.jarosite).toBeCloseTo(0.6);
    expect(g.smectite).toBeCloseTo(0.25);
    expect(g.goethite).toBeCloseTo(0.05);
    expect(g.other).toBeCloseTo(0.02);
    expect(g.background).toBeCloseTo(0.08);
    expect(abundanceGroups({}).every((x) => x.value === null)).toBe(true);
  });

  it('aligns GP series and ±2σ bands by epoch', () => {
    const rows = kinematicRows(KINEMATICS);
    expect(rows).toHaveLength(7);
    expect(rows[6]).toMatchObject({ observed: -20, smooth: -19.8, smoothBand: [-20.6, -19.0], velocity: -200, velocityBand: [-215, -185] });
    expect(rows[0].t).toBe(Date.UTC(2024, 0, 1));
  });

  it('merges fitted Λ, earlier context epochs and the regression band', () => {
    const rows = fukuzonoRows(KINEMATICS, FIT);
    // window 2024-01-25 .. 2024-03-13 (48 days) -> context from 2023-12-08: the first two epochs
    expect(rows.filter((r) => r.context !== null).map((r) => r.context)).toEqual([0.04, 0.033]);
    expect(rows.filter((r) => r.lambda !== null).map((r) => r.lambda)).toEqual([0.05, 0.04, 0.03, 0.02, 0.01]);
    const line = rows.filter((r) => r.fit !== null);
    expect(line.map((r) => r.fit)).toEqual([0.05, 0.01, 0.0, -0.01]);
    expect(line[2].fitBand).toEqual([-0.004, 0.004]);
    expect(rows.map((r) => r.t)).toEqual([...rows.map((r) => r.t)].sort((a, b) => a - b));
    expect(rows.at(-1)!.t).toBe(ms('2024-04-06T00:00:00'));
  });

  it('flags low temporal coherence (γ < 0.65) and decorrelated status', () => {
    expect(coherenceCheck(KINEMATICS).low).toBe(false);
    const low = coherenceCheck({ ...KINEMATICS, temporal_coherence: 0.4 });
    expect(low.low).toBe(true);
    expect(low.message).toContain('γ = 0.40 < 0.65');
    expect(coherenceCheck({ ...KINEMATICS, temporal_coherence: null, status: 'unobservable-low-coherence' }).low).toBe(true);
  });

  it('rates setback epochs and derives the GISTM screening checks', () => {
    const [first, second, third] = DASHBOARD.hydrological_setback.series!;
    expect(setbackLevel(first)).toBe('good');
    expect(setbackLevel(second)).toBe('serious');
    expect(setbackLevel(third)).toBe('critical');
    expect(setbackLevel({ ...first, status: 'no-water', d_crest_m: null })).toBe('unknown');

    const checks = Object.fromEntries(setbackChecks(DASHBOARD.hydrological_setback).map((c) => [c.id, c.level]));
    expect(checks).toEqual({ proximity: 'critical', 'shrink-baseline': 'serious', 'shrink-previous': 'serious', coverage: 'good', crest: 'good' });
    const none = setbackChecks({ facility_id: 'mission', available: false, reason: 'Mission_hydrology.zarr not found' });
    expect(none).toHaveLength(1);
    expect(none[0]).toMatchObject({ level: 'unknown', detail: 'Mission_hydrology.zarr not found' });
  });
});

// --------------------------------------------------------------------------- //
// Rendered panels
// --------------------------------------------------------------------------- //

function Harness({ facility = 'sierrita' }: { facility?: string }) {
  const dash = useDashboard();
  const { registryState, selectFacility } = dash;
  useEffect(() => {
    if (registryState === 'ready') selectFacility(facility);
  }, [registryState, selectFacility, facility]);
  return <Inspector dash={dash} />;
}

async function setup(opts: StubOptions = {}) {
  const fetch = stubFetch(opts);
  render(<Harness />);
  await screen.findByTestId('coordinate-picker');
  return fetch;
}

async function inspect(lat: string, lon: string) {
  const latInput = screen.getByTestId('input-lat');
  const lonInput = screen.getByTestId('input-lon');
  await userEvent.clear(latInput);
  await userEvent.type(latInput, lat);
  await userEvent.clear(lonInput);
  await userEvent.type(lonInput, lon);
  await userEvent.click(screen.getByRole('button', { name: 'Inspect' }));
}

const pixelCalls = (fetch: ReturnType<typeof stubFetch>) => requestedUrls(fetch).filter((u) => u.includes('/pixel?'));

describe('Inspector', () => {
  it('queries the coordinate endpoint with the picked coordinates', async () => {
    const fetch = await setup();
    // prefilled with the facility centre; nothing is requested until a cell is picked
    await waitFor(() => expect(screen.getByTestId('input-lat')).toHaveValue('31.865000'));
    expect(screen.getByTestId('inspector-prompt')).toHaveTextContent('Pick a cell');
    expect(pixelCalls(fetch)).toHaveLength(0);

    await inspect('31.86123', '-111.10456');
    await waitFor(() => expect(pixelCalls(fetch)).toHaveLength(1));
    expect(pixelCalls(fetch)[0]).toBe('/api/facilities/sierrita/pixel?lat=31.86123&lon=-111.10456');
    await waitFor(() => expect(screen.getByTestId('pixel-coords')).toHaveTextContent('31.86123, -111.10456'));
    expect(screen.getByTestId('pixel-regime')).toHaveTextContent('4 · regime4-active-instability');
  });

  it('rejects coordinates outside the facility footprint without calling the API', async () => {
    const fetch = await setup();
    await inspect('32.5', '-111.1');
    expect(screen.getByRole('alert')).toHaveTextContent('Outside the Freeport-McMoRan Sierrita TSF footprint');
    await inspect('abc', '-111.1');
    expect(screen.getByRole('alert')).toHaveTextContent('decimal degrees');
    expect(pixelCalls(fetch)).toHaveLength(0);
  });

  it('plots the spectrum with water-vapour gaps, the NNLS fit, abundances, RMSE and SAI', async () => {
    await setup();
    await inspect('31.86', '-111.1');
    const chart = await screen.findByTestId('chart-spectrum');
    const lines = chart.querySelectorAll('.recharts-line-curve');
    expect(lines).toHaveLength(2); // measured + NNLS fit
    // connectNulls=false: each line is three segments separated by the two excluded windows
    for (const l of lines) expect((l.getAttribute('d') ?? '').match(/M/g)).toHaveLength(3);
    expect(within(chart).getAllByText('H₂O')).toHaveLength(2);
    expect(chart).toHaveTextContent(`${ACTIVE.filter((a) => !a).length} water-vapour channels (1340–1460 nm, 1790–1970 nm)`);
    expect(chart).toHaveTextContent('Measured');
    expect(chart).toHaveTextContent('NNLS fit');

    const bars = screen.getByTestId('chart-abundance');
    expect(bars.querySelectorAll('.recharts-bar-rectangle')).toHaveLength(5);
    expect(bars).toHaveTextContent('60.0 %');
    expect(bars).toHaveTextContent('Smectite (montmorillonite)');
    expect(bars).toHaveTextContent('Fractions of the same cell as the spectrum');

    expect(screen.getByTestId('pixel-sai')).toHaveTextContent('0.470');
    expect(screen.getByTestId('pixel-sai').querySelector('[data-level]')).toHaveAttribute('data-level', 'serious');
    expect(screen.getByTestId('pixel-rmse')).toHaveTextContent('0.0123');
    expect(screen.getByTestId('pixel-rmse')).toHaveTextContent('Fit accepted');
  });

  it('plots displacement, GP velocity and acceleration with 95 % bands and the Fukuzono trend', async () => {
    await setup();
    await inspect('31.86', '-111.1');
    await screen.findByTestId('chart-spectrum');
    await userEvent.click(screen.getByTestId('tab-kinematics'));
    for (const id of ['chart-displacement', 'chart-velocity', 'chart-acceleration', 'chart-fukuzono']) expect(screen.getByTestId(id)).toBeInTheDocument();
    const disp = screen.getByTestId('chart-displacement');
    expect(disp.querySelectorAll('.recharts-area-area')).toHaveLength(1); // ±2σ band
    expect(disp.querySelectorAll('.recharts-scatter-symbol')).toHaveLength(7); // observed epochs
    expect(disp).toHaveTextContent('95 % band (±2σ)');

    const fk = screen.getByTestId('chart-fukuzono');
    expect(within(fk).getByTestId('fukuzono-label')).toHaveTextContent('Estimated change in velocity trend (inverse-velocity extrapolation), not a guaranteed failure date');
    expect(fk).toHaveTextContent('Estimated change in velocity: 2024-03-25 (95 %: 2024-03-20 – 2024-04-01)');
    expect(fk.querySelectorAll('.recharts-reference-area')).toHaveLength(1);
    expect(fk).toHaveTextContent('Λ = 0');
    expect(screen.getByTestId('pixel-coherence')).toHaveTextContent('0.91');
    expect(screen.queryByTestId('coherence-alert')).not.toBeInTheDocument();
  });

  it('shows an error alert and no GP fit for a decorrelated cell (γ < 0.65)', async () => {
    const pixel: PixelResponse = {
      ...PIXEL,
      kinematics: {
        ...KINEMATICS,
        temporal_coherence: 0.4,
        status: 'unobservable-low-coherence',
        displacement_smooth_mm: KINEMATICS.times.map(() => null),
        displacement_smooth_lower_mm: null,
        displacement_smooth_upper_mm: null,
        velocity_mm_yr: KINEMATICS.times.map(() => null),
        acceleration_mm_yr2: null,
        fukuzono: null,
      },
    };
    await setup({ pixel });
    await inspect('31.86', '-111.1');
    await screen.findByTestId('chart-spectrum');
    await userEvent.click(screen.getByTestId('tab-kinematics'));
    expect(screen.getByTestId('coherence-alert')).toHaveTextContent('Low temporal coherence (γ = 0.40 < 0.65): the GP fit is unreliable');
    expect(screen.getByTestId('pixel-coherence').querySelector('[data-level]')).toHaveAttribute('data-level', 'critical');
    expect(screen.getByTestId('chart-displacement')).toBeInTheDocument(); // raw observations still shown
    expect(screen.getByTestId('velocity-empty')).toHaveTextContent('(low coherence)');
    expect(screen.queryByTestId('chart-acceleration')).not.toBeInTheDocument();
    expect(screen.getByTestId('fukuzono-empty')).toHaveTextContent('Decorrelated cell: not modelled.');
  });

  it('handles cells without spectral or kinematic products', async () => {
    const pixel: PixelResponse = {
      ...PIXEL,
      spectral: null,
      kinematics: null,
      classification: null,
      unavailable: { spectral: 'Sierrita_spectral.zarr not found', kinematics: 'point lies outside the Sierrita_kinematics.zarr grid' },
    };
    await setup({ pixel });
    await inspect('31.86', '-111.1');
    expect(await screen.findByTestId('spectral-empty')).toHaveTextContent('No spectral product at this cell: Sierrita_spectral.zarr not found');
    expect(screen.queryByTestId('pixel-regime')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('tab-kinematics'));
    expect(screen.getByTestId('kinematics-empty')).toHaveTextContent('outside the Sierrita_kinematics.zarr grid');
  });

  it('shows per-product reasons when the API has no data at all for the cell (404)', async () => {
    await setup({
      pixelUnavailable: {
        spectral: 'Sierrita_spectral.zarr not found (run the `unmix` command for this facility)',
        kinematics: 'Sierrita_kinematics.zarr not found (run the `kinematics` command for this facility)',
      },
    });
    await inspect('31.86', '-111.1');
    expect(await screen.findByTestId('spectral-empty')).toHaveTextContent('run the `unmix` command');
    expect(screen.getByTestId('pixel-panel')).toHaveTextContent('kinematics: Sierrita_kinematics.zarr not found');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('tab-kinematics'));
    expect(screen.getByTestId('kinematics-empty')).toHaveTextContent('run the `kinematics` command');
  });

  it('handles a product without a stored spectrum, empty fractions and an empty displacement series', async () => {
    const pixel: PixelResponse = {
      ...PIXEL,
      spectral: { ...PIXEL.spectral!, spectrum: null, spectrum_unavailable: 'this spectral product has no reflectance cube', abundance: {} },
      kinematics: { ...KINEMATICS, displacement_mm: KINEMATICS.times.map(() => null) },
    };
    await setup({ pixel });
    await inspect('31.86', '-111.1');
    expect(await screen.findByTestId('spectrum-empty')).toHaveTextContent('no reflectance cube');
    expect(screen.getByTestId('abundance-empty')).toBeInTheDocument();
    expect(screen.getByTestId('pixel-rmse')).toHaveTextContent('0.0123'); // the bilinear point values remain
    await userEvent.click(screen.getByTestId('tab-kinematics'));
    expect(screen.getByTestId('displacement-empty')).toBeInTheDocument();
  });

  it('plots the D_crest history with alert levels and GISTM screening checks', async () => {
    await setup();
    await userEvent.click(screen.getByTestId('tab-hydrology'));
    const chart = await screen.findByTestId('chart-setback');
    expect(chart).toHaveTextContent('15 m proximity alert');
    expect(chart.querySelectorAll('.recharts-line-dots circle')).toHaveLength(3);
    const rows = within(screen.getByTestId('setback-table')).getAllByRole('row');
    expect(rows).toHaveLength(4);
    expect(within(rows[3]).getByText('3 alerts').closest('[data-level]')).toHaveAttribute('data-level', 'critical');
    expect(rows[3]).toHaveTextContent('Pond within 15 m of the crest');
    const checks = screen.getByTestId('gistm-checks');
    expect(checks.querySelector('[data-check="proximity"]')).toHaveAttribute('data-level', 'critical');
    expect(checks.querySelector('[data-check="coverage"]')).toHaveAttribute('data-level', 'good');
    expect(checks).toHaveTextContent('not a GISTM compliance determination');
    expect(screen.getByTestId('gistm-issues')).toHaveTextContent('Principle 6:');
  });

  it('handles a facility without a setback product, and an unreachable summary', async () => {
    const noSetback: DashboardSummary = {
      ...DASHBOARD,
      hydrological_setback: { facility_id: 'sierrita', available: false, reason: 'Sierrita_hydrology.zarr not found' },
      gistm_screening: { candidate_issues: [], recommendations: [], data_limitations: [] },
    };
    await setup({ dashboard: noSetback });
    await userEvent.click(screen.getByTestId('tab-hydrology'));
    expect(await screen.findByTestId('setback-empty')).toHaveTextContent('Sierrita_hydrology.zarr not found');
    expect(screen.getByTestId('gistm-checks').querySelector('[data-check="setback-data"]')).toHaveAttribute('data-level', 'unknown');
    expect(screen.queryByTestId('gistm-issues')).not.toBeInTheDocument();
  });

  it('reports a failed summary request in the hydrology panel', async () => {
    await setup({ dashboard: { status: 503, detail: 'products unreadable' } });
    await userEvent.click(screen.getByTestId('tab-hydrology'));
    expect(await screen.findByTestId('hydrology-error')).toHaveTextContent('products unreadable');
  });
});
