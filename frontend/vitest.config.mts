import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // Recharts SVG rendering in jsdom is slow under parallel load; keep a generous per-test budget.
    testTimeout: 20_000,
    // deck.gl / luma.gl / loaders.gl ship ESM that Node cannot load directly; let Vite transform them.
    server: { deps: { inline: [/@deck\.gl/, /@luma\.gl/, /@loaders\.gl/, /@math\.gl/, /wgsl_reflect/] } },
  },
});
