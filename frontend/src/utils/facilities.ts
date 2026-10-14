/** Facility footprints and alert colours (no deck.gl imports, so safe for server rendering). */
import type { Alert, Facility, HazardLevel } from './api';

export type Bbox = [number, number, number, number];

// --------------------------------------------------------------------------- //
// Facilities
// --------------------------------------------------------------------------- //

export type AlertColorName = 'green' | 'yellow' | 'red' | 'unknown';

export const ALERT_COLORS: Record<AlertColorName, [number, number, number]> = {
  green: [34, 197, 94],
  yellow: [250, 204, 21],
  red: [239, 68, 68],
  unknown: [148, 163, 184],
};

/** Green = no hazard, yellow = watch/alert tiers, red = Tier-3 critical, grey = no classification yet. */
export function alertColorName(hazard: HazardLevel | null | undefined): AlertColorName {
  switch (hazard) {
    case 'NONE':
      return 'green';
    case 'WATCH_TIER1':
    case 'ALERT_TIER2':
      return 'yellow';
    case 'CRITICAL_TIER3':
      return 'red';
    default:
      return 'unknown';
  }
}

export const ALERT_LABELS: Record<AlertColorName, string> = {
  green: 'No hazard flagged',
  yellow: 'Watch / alert tier',
  red: 'Tier-3 critical',
  unknown: 'Not yet classified',
};

export function facilityBbox(f: Facility): Bbox {
  return [f.bbox_xmin, f.bbox_ymin, f.bbox_xmax, f.bbox_ymax];
}

export interface FacilityProps {
  id: string;
  name: string;
  country: string;
  mine_type: string;
  embankment_type: string;
  status: string;
  hazard_level: HazardLevel | null;
  alert_color: AlertColorName;
  alert_timestamp: string | null;
  synthetic_inputs: boolean | null;
}

/** Facility footprints (the registry bbox polygons) as GeoJSON, tagged with the latest alert. */
export function facilitiesGeoJson(
  facilities: Facility[],
  latestAlerts: Record<string, Alert | null>,
): GeoJSON.FeatureCollection<GeoJSON.Polygon, FacilityProps> {
  return {
    type: 'FeatureCollection',
    features: facilities.map((f) => {
      const [x0, y0, x1, y1] = facilityBbox(f);
      const alert = latestAlerts[f.id] ?? null;
      const synthetic = alert?.diagnostic_evidence?.contains_synthetic_or_mock_inputs;
      return {
        type: 'Feature',
        geometry: { type: 'Polygon', coordinates: [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] },
        properties: {
          id: f.id,
          name: f.name,
          country: f.country,
          mine_type: f.mine_type,
          embankment_type: f.embankment_type,
          status: f.status,
          hazard_level: alert?.hazard_level ?? null,
          alert_color: alertColorName(alert?.hazard_level),
          alert_timestamp: alert?.timestamp ?? null,
          synthetic_inputs: typeof synthetic === 'boolean' ? synthetic : null,
        },
      };
    }),
  };
}
