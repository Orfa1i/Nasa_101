/**
 * Sidebar + MapViewer + useDashboard together, with the fetch stub serving API fixtures.
 * WebGL is unavailable in jsdom, so <DeckGL> is replaced by a stub that records its props; the
 * layers it receives are the real deck.gl layer objects built by the app. Real rendering is
 * covered by the Playwright tests in e2e/.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';

import MapViewer from '@/components/MapViewer';
import Sidebar from '@/components/Sidebar';
import { useDashboard } from '@/hooks/useDashboard';

import { PIXEL, SIERRITA_BBOX, requestedUrls, stubFetch } from './fixtures';

type DeckProps = {
  layers: { id: string; props: Record<string, unknown> }[];
  viewState: { longitude: number; latitude: number; zoom: number; pitch: number };
  onClick: (info: unknown) => void;
  onHover: (info: unknown) => void;
  getTooltip: (info: unknown) => { html: string } | null;
  children?: ReactNode;
};
const deck: { props: DeckProps | null } = { props: null };

vi.mock('@deck.gl/react', () => ({
  default: (props: DeckProps) => {
    deck.props = props;
    return <div data-testid="deck-stub">{props.children}</div>;
  },
}));
vi.mock('@/components/Basemap', () => ({ default: () => null, basemapProvider: () => 'MapLibre GL' }));

function App() {
  const dash = useDashboard();
  return (
    <>
      <Sidebar dash={dash} />
      <div style={{ width: 1000, height: 700 }}>
        <MapViewer dash={dash} />
      </div>
    </>
  );
}

const rawLayerIds = () => deck.props!.layers.map((l) => l.id);
/** Layer ids without the `@draped` suffix that 3D terrain (on by default) adds. */
const layerIds = () => rawLayerIds().map((id) => id.replace('@draped', ''));
const layer = (id: string) => deck.props!.layers.find((l) => l.id.replace('@draped', '') === id)!;

beforeAll(() => {
  // jsdom has no layout: give the map container a size so the camera can frame bboxes.
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 1000 });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 700 });
  window.matchMedia = ((q: string) => ({ matches: q.includes('reduce') || q.includes('min-width'), media: q, addEventListener() {}, removeEventListener() {} })) as never;
});

beforeEach(() => {
  deck.props = null;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function selectSierrita() {
  await userEvent.selectOptions(screen.getByTestId('facility-select'), 'sierrita');
  await waitFor(() => expect(layerIds()).toContain('matrix-anomaly_regime'));
}

describe('dashboard', () => {
  it('renders the facility picker, alert colours and the world view', async () => {
    stubFetch();
    render(<App />);
    const list = await screen.findByRole('list', { name: 'Facilities by alert status' });
    await waitFor(() => expect(within(list).getAllByRole('button')).toHaveLength(2));
    await waitFor(() => expect(within(list).getByRole('button', { name: /Sierrita/ }).querySelector('[data-alert]')).toHaveAttribute('data-alert', 'red'));
    expect(within(list).getByRole('button', { name: /Mission/ }).querySelector('[data-alert]')).toHaveAttribute('data-alert', 'unknown');
    expect(screen.getByTestId('watermark-headline')).toHaveTextContent('NO MATRIX DATA LOADED');
    expect(layerIds()).toEqual(expect.arrayContaining(['facilities', 'facility-markers']));
    expect(screen.queryByTestId('quality-panel')).not.toBeInTheDocument();
    expect(screen.getByText('Select a facility to load its OPERA epochs.')).toBeInTheDocument();
  });

  it('selecting a facility centres the 3D camera on its bbox and loads matrices, mask and quality layers', async () => {
    const fetch = stubFetch();
    render(<App />);
    await screen.findByRole('option', { name: /Sierrita/ });
    await selectSierrita();

    const vs = deck.props!.viewState;
    expect(vs.longitude).toBeCloseTo((SIERRITA_BBOX[0] + SIERRITA_BBOX[2]) / 2, 3);
    expect(vs.latitude).toBeCloseTo((SIERRITA_BBOX[1] + SIERRITA_BBOX[3]) / 2, 2);
    expect(vs.pitch).toBe(50);
    expect(vs.zoom).toBeGreaterThan(10);

    const urls = requestedUrls(fetch);
    for (const layer of ['anomaly_regime', 'spectral_rmse', 'coherence', 'observability_mask'])
      expect(urls.some((u) => u.includes(`/matrices?layer=${layer}`))).toBe(true);
    await waitFor(() => expect(rawLayerIds()).toEqual(expect.arrayContaining(['terrain', 'matrix-anomaly_regime@draped', 'observability-mask@draped'])));
    expect(screen.getByTestId('watermark')).toHaveAttribute('data-provenance', 'simulated');
    expect(screen.getByTestId('simulated-backdrop')).toBeInTheDocument();

    // Data quality: RMSE median 0.0100, 25 % > 0.05; coherence median 0.90, 25 % < 0.65.
    expect(screen.getByTestId('quality-rmse')).toHaveTextContent('0.0100');
    expect(screen.getByTestId('quality-rmse')).toHaveTextContent('25.0 %');
    expect(screen.getByTestId('quality-coherence')).toHaveTextContent('0.90');
    expect(screen.getByTestId('quality-coherence')).toHaveTextContent('25.0 %');
    expect(screen.getByTestId('quality-panel')).toHaveTextContent('Masked: 1 vegetated, 4 decorrelated cells.');
  });

  it('switches overlay matrices and their legends', async () => {
    const fetch = stubFetch();
    render(<App />);
    await screen.findByRole('option', { name: /Sierrita/ });
    await selectSierrita();
    expect(screen.getByTestId('legend-anomaly_regime')).toBeInTheDocument();

    await userEvent.click(screen.getByTestId('toggle-sai'));
    await waitFor(() => expect(layerIds().filter((i) => i.startsWith('matrix-'))).toEqual(['matrix-sai', 'matrix-anomaly_regime']));
    expect(requestedUrls(fetch).some((u) => u.includes('layer=sai'))).toBe(true);
    expect(screen.getByTestId('legend-sai')).toHaveTextContent('SAI 0.25');

    await userEvent.click(screen.getByTestId('toggle-anomaly_regime'));
    await waitFor(() => expect(layerIds().filter((i) => i.startsWith('matrix-'))).toEqual(['matrix-sai']));
    expect(screen.queryByTestId('legend-anomaly_regime')).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId('toggle-flow_accumulation'));
    await waitFor(() => expect(layerIds()).toContain('matrix-flow_accumulation'));

    // mask and terrain toggles
    await userEvent.click(screen.getByTestId('toggle-mask'));
    expect(layerIds().some((i) => i.startsWith('observability-mask'))).toBe(false);
    await userEvent.click(screen.getByTestId('toggle-terrain'));
    expect(rawLayerIds()).not.toContain('terrain');
    expect(rawLayerIds()).toContain('matrix-sai'); // flat again

    // The source matrix of the SAI layer is the one the API returned.
    const sai = layer('matrix-sai');
    expect(sai.props.bounds).toEqual(SIERRITA_BBOX);
  });

  it('disables overlays a facility has no product for', async () => {
    stubFetch();
    render(<App />);
    await screen.findByRole('option', { name: /Mission/ });
    await userEvent.selectOptions(screen.getByTestId('facility-select'), 'mission');
    await waitFor(() => expect(screen.getByTestId('toggle-sai')).toBeDisabled());
    expect(screen.getByTestId('status-anomaly_regime')).toHaveTextContent('n/a');
    expect(screen.getByTestId('watermark')).toHaveAttribute('data-provenance', 'none');
    expect(screen.getByText('No time-dependent OPERA products for this facility.')).toBeInTheDocument();
  });

  it('the OPERA timeline selects the epoch used for time-dependent matrices', async () => {
    const fetch = stubFetch();
    render(<App />);
    await screen.findByRole('option', { name: /Sierrita/ });
    await selectSierrita();
    await userEvent.click(screen.getByTestId('toggle-displacement'));
    await waitFor(() => expect(screen.getByTestId('legend-displacement')).toHaveTextContent('epoch 2023-02-06'));
    const timeline = screen.getByTestId('timeline');
    expect(within(timeline).getByTestId('epoch-label')).toHaveTextContent('2023-03-05');
    expect(timeline).toHaveTextContent('4 / 4 (latest)');

    fireEvent.change(screen.getByTestId('epoch-slider'), { target: { value: '0' } });
    expect(within(timeline).getByTestId('epoch-label')).toHaveTextContent('2023-01-13');
    expect(timeline).toHaveTextContent('DISP-S1');
    await waitFor(() => expect(requestedUrls(fetch).some((u) => u.includes('layer=displacement&t=2023-01-13'))).toBe(true));
    await waitFor(() => expect(screen.getByTestId('legend-displacement')).toHaveTextContent('epoch 2023-01-13'));

    await userEvent.click(within(timeline).getByRole('button', { name: /Next/ }));
    expect(within(timeline).getByTestId('epoch-label')).toHaveTextContent('2023-01-25');
    expect(timeline).toHaveTextContent('DISP-S1 + DSWx');
    await userEvent.click(within(timeline).getByRole('button', { name: 'Latest' }));
    expect(timeline).toHaveTextContent('(latest)');
  });

  it('map clicks load the cell into the sidebar; hovering shows RMSE and coherence', async () => {
    const fetch = stubFetch();
    render(<App />);
    await screen.findByRole('option', { name: /Sierrita/ });
    await selectSierrita();
    const regime = layer('matrix-anomaly_regime');
    const info = { layer: regime, bitmap: { uv: [0.1, 0.1] }, coordinate: [0, 0], object: null };

    act(() => deck.props!.onHover(info));
    // uv (0.1, 0.1) -> lon -111.112, lat 31.893: cell (0, 0) with RMSE 0.01 and coherence 0.9
    expect(screen.getByTestId('quality-hover')).toHaveTextContent('cursor RMSE 0.0100 · coh 0.90');
    const tip = deck.props!.getTooltip(info)!;
    expect(tip.html).toContain('Anomaly regimes: 4');

    act(() => deck.props!.onClick(info));
    await waitFor(() => expect(screen.getByTestId('pixel-panel')).toBeInTheDocument());
    const pixelUrl = requestedUrls(fetch).find((u) => u.includes('/pixel?'))!;
    const q = new URL(pixelUrl, 'http://x').searchParams;
    expect(Number(q.get('lat'))).toBeCloseTo(31.893, 9);
    expect(Number(q.get('lon'))).toBeCloseTo(-111.112, 9);
    const panel = screen.getByTestId('pixel-panel');
    await waitFor(() => expect(within(panel).getByTestId('pixel-regime')).toHaveTextContent(`4 · ${PIXEL.classification!.regime_meaning}`));
    expect(panel).toHaveTextContent('2025-01-13 [95 %: 2024-12-31 – 2025-01-27]');
    // The click populated the inspector charts.
    expect(screen.getByTestId('chart-spectrum')).toBeInTheDocument();
    expect(screen.getByTestId('pixel-rmse')).toHaveTextContent('0.0123');
    await userEvent.click(screen.getByTestId('tab-kinematics'));
    expect(screen.getByTestId('pixel-coherence')).toHaveTextContent('0.91');
    expect(screen.getByTestId('chart-fukuzono')).toBeInTheDocument();
    expect(panel).toHaveTextContent('hydrology: Sierrita_hydrology.zarr not found');
    expect(rawLayerIds()).toContain('picked-point@draped');

    // A click outside the bbox does nothing; a click on another facility's marker selects it.
    const before = requestedUrls(fetch).length;
    act(() => deck.props!.onClick({ layer: null, coordinate: [-100, 40], object: null }));
    expect(requestedUrls(fetch).length).toBe(before);
    const markers = layer('facility-markers');
    const mission = (markers.props.data as { properties: { id: string } }[])[0];
    act(() => deck.props!.onClick({ layer: markers, object: mission, coordinate: [-111.04, 31.975] }));
    await waitFor(() => expect(screen.getByTestId('facility-select')).toHaveValue('mission'));
  });

  it('facility tooltips expose the registry attributes and alert', async () => {
    stubFetch();
    render(<App />);
    await screen.findByRole('option', { name: /Sierrita/ });
    await waitFor(() => expect(screen.getByRole('button', { name: /Sierrita/ }).querySelector('[data-alert]')).toHaveAttribute('data-alert', 'red'));
    const facilities = layer('facilities');
    const feature = (facilities.props.data as { features: { properties: { id: string } }[] }).features[0];
    const html = deck.props!.getTooltip({ layer: facilities, object: feature, coordinate: [-111.08, 31.865] })!.html;
    expect(html).toContain('Freeport-McMoRan Sierrita TSF');
    expect(html).toContain('copper-molybdenum');
    expect(html).toContain('CRITICAL_TIER3 (Tier-3 critical)');
    expect(html).toContain('SYNTHETIC/MOCK');
  });

  it('reports an unavailable backend with a retry', async () => {
    const fetch = stubFetch({ facilitiesStatus: 503 });
    render(<App />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Backend unavailable: 503 /api/facilities: database offline');
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(requestedUrls(fetch).filter((u) => u === '/api/facilities')).toHaveLength(2);
  });
});
