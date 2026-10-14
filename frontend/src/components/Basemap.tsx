/**
 * 2D basemap under the deck.gl canvas: Mapbox GL when a token is configured, otherwise MapLibre GL
 * with an open CARTO style. Rendered as a child of <DeckGL>, which drives its view state.
 * Each library is code-split, so only the one in use is downloaded.
 */
import dynamic from 'next/dynamic';

const MAPLIBRE_STYLE = process.env.NEXT_PUBLIC_MAP_STYLE || 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';
const MAPBOX_STYLE = process.env.NEXT_PUBLIC_MAP_STYLE || 'mapbox://styles/mapbox/satellite-streets-v12';

type BasemapProps = { mapboxToken?: string; onError?: (message: string) => void };

const MapboxMap = dynamic(
  () =>
    import('react-map-gl/mapbox').then(({ Map }) => {
      function MapboxBasemap({ mapboxToken, onError }: BasemapProps) {
        return (
          <Map
            reuseMaps
            mapboxAccessToken={mapboxToken}
            mapStyle={MAPBOX_STYLE}
            attributionControl={false}
            onError={(e) => onError?.(e.error?.message ?? 'Mapbox basemap error')}
          />
        );
      }
      return MapboxBasemap;
    }),
  { ssr: false },
);

const MaplibreMap = dynamic(
  () =>
    import('react-map-gl/maplibre').then(({ Map }) => {
      function MaplibreBasemap({ onError }: BasemapProps) {
        return (
          <Map
            reuseMaps
            mapStyle={MAPLIBRE_STYLE}
            attributionControl={false}
            onError={(e) => onError?.(e.error?.message ?? 'MapLibre basemap error')}
          />
        );
      }
      return MaplibreBasemap;
    }),
  { ssr: false },
);

export default function Basemap({ mapboxToken, onError }: BasemapProps) {
  return mapboxToken ? <MapboxMap mapboxToken={mapboxToken} onError={onError} /> : <MaplibreMap onError={onError} />;
}

export function basemapProvider(mapboxToken?: string): 'Mapbox GL' | 'MapLibre GL' {
  return mapboxToken ? 'Mapbox GL' : 'MapLibre GL';
}
