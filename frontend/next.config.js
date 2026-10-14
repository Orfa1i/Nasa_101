/** @type {import('next').NextConfig} */

// The browser calls same-origin /api/*; Next proxies it to the FastAPI backend, so no CORS
// set-up is needed in development or `next start`.
const API_URL = (process.env.LITHOCREEP_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');

module.exports = {
  reactStrictMode: true,
  env: {
    // The playbook exports MAPBOX_ACCESS_TOKEN; expose it to the browser bundle. Without a token
    // the map falls back to MapLibre GL with open basemap and terrain tiles.
    NEXT_PUBLIC_MAPBOX_TOKEN: process.env.NEXT_PUBLIC_MAPBOX_TOKEN || process.env.MAPBOX_ACCESS_TOKEN || '',
  },
  async rewrites() {
    return [{ source: '/api/:path*', destination: `${API_URL}/api/:path*` }];
  },
};
