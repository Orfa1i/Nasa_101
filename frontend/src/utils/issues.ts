/**
 * In-app issues log: API outages, fallbacks taken (Mapbox -> MapLibre, terrain -> flat, cached
 * registry) and crashed panels are recorded here and shown in the sidebar, so failures are
 * reported to the user instead of failing silently.
 */
import { useSyncExternalStore } from 'react';

export type IssueSource = 'api' | 'basemap' | 'terrain' | 'cache' | 'render';

export interface Issue {
  id: number;
  at: number;
  source: IssueSource;
  message: string;
  detail?: string;
  /** a fallback was taken, so the app kept working */
  fallback?: string;
  count: number;
}

const MAX = 50;
let issues: Issue[] = [];
let seq = 0;
const listeners = new Set<() => void>();

export function reportIssue(i: { source: IssueSource; message: string; detail?: string; fallback?: string }): void {
  const same = issues.find((x) => x.source === i.source && x.message === i.message && x.detail === i.detail);
  if (same) {
    issues = issues.map((x) => (x === same ? { ...x, at: Date.now(), count: x.count + 1 } : x));
  } else {
    issues = [{ ...i, id: ++seq, at: Date.now(), count: 1 }, ...issues].slice(0, MAX);
  }
  listeners.forEach((l) => l());
  if (typeof console !== 'undefined') console.warn(`LithoCreep [${i.source}] ${i.message}${i.fallback ? ` -> ${i.fallback}` : ''}`);
}

export function clearIssues(): void {
  issues = [];
  listeners.forEach((l) => l());
}

export function getIssues(): Issue[] {
  return issues;
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

const EMPTY: Issue[] = [];

export function useIssues(): Issue[] {
  return useSyncExternalStore(subscribe, getIssues, () => EMPTY);
}
