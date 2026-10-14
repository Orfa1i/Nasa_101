import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach } from 'vitest';

// Recharts renders real SVG in jsdom, which can take a few hundred ms per panel.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => cleanup());
