/** Contains a crashed panel: the rest of the dashboard keeps working and the error is reported. */
import { Component, type ErrorInfo, type ReactNode } from 'react';

import { reportIssue } from '@/utils/issues';

interface Props {
  name: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    reportIssue({ source: 'render', message: `${this.props.name} crashed: ${error.message}`, detail: info.componentStack?.split('\n').slice(0, 3).join(' ') });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" data-testid={`crashed-${this.props.name.toLowerCase().replace(/\s+/g, '-')}`} className="m-2 rounded border border-red-500/50 bg-red-950/50 p-3 text-xs text-red-100">
        <p className="font-semibold">{this.props.name} stopped working.</p>
        <p className="mt-1 text-red-200/80">{this.state.error.message}</p>
        <button type="button" className="mt-2 rounded bg-red-800 px-2 py-1 text-white hover:bg-red-700" onClick={() => this.setState({ error: null })}>
          Retry
        </button>
      </div>
    );
  }
}
