import dynamic from 'next/dynamic';
import { useState } from 'react';

import Sidebar from '@/components/Sidebar';
import { useDashboard } from '@/hooks/useDashboard';

// deck.gl needs WebGL and `window`: render the map on the client only.
const MapViewer = dynamic(() => import('@/components/MapViewer'), {
  ssr: false,
  loading: () => <div className="flex h-full items-center justify-center text-sm text-slate-400">Loading map…</div>,
});

const MAPBOX_TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN || undefined;

export default function Home() {
  const dash = useDashboard();
  const [panelOpen, setPanelOpen] = useState(true);
  return (
    <div className="flex h-dvh w-full flex-col overflow-hidden lg:flex-row">
      <main className="relative order-1 min-h-0 flex-1 lg:order-2" aria-label="Map">
        <MapViewer dash={dash} mapboxToken={MAPBOX_TOKEN} />
        <button
          type="button"
          data-testid="panel-toggle"
          aria-expanded={panelOpen}
          aria-controls="dashboard-panel"
          onClick={() => setPanelOpen((o) => !o)}
          className="absolute bottom-7 right-2 rounded bg-slate-800/90 px-3 py-1.5 text-xs text-slate-100 shadow lg:hidden"
        >
          {panelOpen ? 'Hide controls' : 'Show controls'}
        </button>
      </main>
      <div
        id="dashboard-panel"
        className={`order-2 w-full shrink-0 overflow-y-auto border-t border-slate-800 lg:order-1 lg:block lg:h-full lg:w-[22rem] lg:border-r lg:border-t-0 xl:w-[26rem] 2xl:w-[30rem] ${
          panelOpen ? 'h-[45dvh]' : 'hidden'
        }`}
      >
        <Sidebar dash={dash} className="min-h-full" />
      </div>
    </div>
  );
}
