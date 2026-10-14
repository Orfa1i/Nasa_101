/**
 * 2D basemap under the deck.gl canvas: Mapbox GL when a token is configured, otherwise MapLibre GL
 * with an open CARTO style. Rendered as a child of <DeckGL>, which drives its view state.
 * Each library is code-split, so only the one in use is downloaded.
 */
import dynamic from 'next/dynamic';
import { useEffect, useState } from 'react';

import { reportIssue } from '@/utils/issues';

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

/**
 * Mapbox outage (bad token, quota, network): fall back to MapLibre GL with the open style and
 * record the fallback in the issues log. A MapLibre failure leaves the overlays without a basemap.
 */
export default function Basemap({ mapboxToken, onError, onProvider }: BasemapProps & { onProvider?: (name: 'Mapbox GL' | 'MapLibre GL') => void }) {
  const [mapboxFailed, setMapboxFailed] = useState(false);
  const useMapbox = !!mapboxToken && !mapboxFailed;
  useEffect(() => onProvider?.(useMapbox ? 'Mapbox GL' : 'MapLibre GL'), [useMapbox, onProvider]);
  if (useMapbox) {
    return (
      <MapboxMap
        mapboxToken={mapboxToken}
        onError={(message) => {
          reportIssue({ source: 'basemap', message: `Mapbox basemap failed: ${message}`, fallback: 'MapLibre GL with open basemap tiles' });
          setMapboxFailed(true);
        }}
      />
    );
  }
  return (
    <MaplibreMap
      onError={(message) => {
        reportIssue({ source: 'basemap', message: `Basemap unavailable: ${message}`, fallback: 'overlays drawn without a basemap' });
        onError?.(message);
      }}
    />
  );
}

export function basemapProvider(mapboxToken?: string): 'Mapbox GL' | 'MapLibre GL' {
  return mapboxToken ? 'Mapbox GL' : 'MapLibre GL';
}
