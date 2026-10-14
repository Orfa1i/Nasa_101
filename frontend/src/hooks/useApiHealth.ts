/** Polls GET /api/health so a backend outage (and its recovery) is shown to the user. */
import { useEffect, useRef, useState } from 'react';

import { api } from '@/utils/api';

export interface ApiHealth {
  state: 'unknown' | 'ok' | 'down';
  since: number | null;
  lastError: string | null;
}

export function useApiHealth(intervalMs = 20_000, onRecover?: () => void): ApiHealth {
  const [health, setHealth] = useState<ApiHealth>({ state: 'unknown', since: null, lastError: null });
  const recover = useRef(onRecover);
  recover.current = onRecover;

  useEffect(() => {
    let alive = true;
    let ctrl: AbortController | null = null;
    const check = async () => {
      ctrl?.abort();
      ctrl = new AbortController();
      try {
        await api.health(ctrl.signal);
        if (!alive) return;
        setHealth((h) => {
          if (h.state === 'down') recover.current?.();
          return h.state === 'ok' ? h : { state: 'ok', since: Date.now(), lastError: null };
        });
      } catch (err) {
        if (!alive || (err instanceof Error && err.name === 'AbortError')) return;
        setHealth((h) => ({ state: 'down', since: h.state === 'down' ? h.since : Date.now(), lastError: err instanceof Error ? err.message : String(err) }));
      }
    };
    void check();
    const timer = setInterval(check, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
      ctrl?.abort();
    };
  }, [intervalMs]);
  return health;
}
