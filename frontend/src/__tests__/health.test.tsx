/** Backend outage banner: the health poll detects the outage and the recovery. */
import { act, render, screen } from '@testing-library/react';

import { useApiHealth } from '@/hooks/useApiHealth';

import { stubFetch } from './fixtures';

function Probe({ onRecover }: { onRecover: () => void }) {
  const h = useApiHealth(20_000, onRecover);
  return <p data-testid="health" data-state={h.state} />;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('reports a backend outage and recovers on the next poll', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  stubFetch({ healthDown: true });
  const onRecover = vi.fn();
  render(<Probe onRecover={onRecover} />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(screen.getByTestId('health')).toHaveAttribute('data-state', 'down');

  stubFetch(); // backend back
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20_000);
  });
  expect(screen.getByTestId('health')).toHaveAttribute('data-state', 'ok');
  expect(onRecover).toHaveBeenCalledTimes(1);
});
