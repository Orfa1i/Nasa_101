/** Mapbox outage: the basemap falls back to MapLibre GL and the fallback is reported. */
import { render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';

import Basemap from '@/components/Basemap';
import { getIssues } from '@/utils/issues';

type MapProps = { onError?: (e: { error?: { message?: string } }) => void; mapStyle?: string };

vi.mock('react-map-gl/mapbox', () => ({
  // A Mapbox outage: the style request is rejected (bad token / quota / network).
  Map: ({ onError }: MapProps) => {
    useEffect(() => onError?.({ error: { message: '401 Unauthorized: invalid access token' } }), [onError]);
    return <div data-testid="mapbox-map" />;
  },
}));
vi.mock('react-map-gl/maplibre', () => ({
  Map: ({ mapStyle }: MapProps) => <div data-testid="maplibre-map" data-style={mapStyle} />,
}));

describe('Basemap', () => {
  it('uses MapLibre GL with the open style when no Mapbox token is configured', async () => {
    const provider = vi.fn();
    render(<Basemap onProvider={provider} />);
    expect(await screen.findByTestId('maplibre-map')).toHaveAttribute('data-style', expect.stringContaining('basemaps.cartocdn.com'));
    expect(provider).toHaveBeenLastCalledWith('MapLibre GL');
    expect(getIssues()).toHaveLength(0);
  });

  it('falls back from Mapbox GL to MapLibre GL when Mapbox fails, and reports it', async () => {
    const provider = vi.fn();
    render(<Basemap mapboxToken="pk.invalid" onProvider={provider} />);
    expect(await screen.findByTestId('maplibre-map')).toBeInTheDocument();
    expect(screen.queryByTestId('mapbox-map')).not.toBeInTheDocument();
    await waitFor(() => expect(provider).toHaveBeenLastCalledWith('MapLibre GL'));
    expect(provider).toHaveBeenCalledWith('Mapbox GL');
    expect(getIssues()[0]).toMatchObject({
      source: 'basemap',
      message: 'Mapbox basemap failed: 401 Unauthorized: invalid access token',
      fallback: 'MapLibre GL with open basemap tiles',
    });
  });
});
