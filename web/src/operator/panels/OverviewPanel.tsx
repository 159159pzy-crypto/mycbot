import { useEffect, useState } from 'react';

import type { MetricsView, OperatorClient, UsagePoint } from '../api';

type OverviewState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; usage: UsagePoint[]; metrics: MetricsView };

function Sparkline({ points }: { points: UsagePoint[] }) {
  if (points.length === 0) {
    return <p className="op-empty">No turns recorded yet.</p>;
  }
  const width = 240;
  const height = 48;
  const peak = Math.max(...points.map((point) => point.tokens), 1);
  const step = points.length > 1 ? width / (points.length - 1) : width;
  const path = points
    .map((point, index) => {
      const x = points.length > 1 ? index * step : width / 2;
      const y = height - (point.tokens / peak) * (height - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return (
    <svg
      className="op-sparkline"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Token usage over ${points.length} days, peaking at ${peak} tokens`}
    >
      <polyline points={path} fill="none" />
    </svg>
  );
}

export function OverviewPanel({ client }: { client: OperatorClient }) {
  const [state, setState] = useState<OverviewState>({ kind: 'loading' });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [usage, metrics] = await Promise.all([
          client.get<{ usage: UsagePoint[] }>('/operator/usage'),
          client.get<MetricsView>('/operator/metrics'),
        ]);
        if (!cancelled) {
          setState({ kind: 'ready', usage: usage.usage, metrics });
        }
      } catch (error) {
        if (!cancelled) {
          setState({ kind: 'error', message: String(error) });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  if (state.kind === 'loading') {
    return <p className="op-empty">Loading usage…</p>;
  }
  if (state.kind === 'error') {
    return <p className="op-error">{state.message}</p>;
  }
  const totalTokens = state.usage.reduce((sum, point) => sum + point.tokens, 0);
  const totalTurns = state.usage.reduce((sum, point) => sum + point.turns, 0);
  const outcomes = Object.entries(state.metrics.turns.by_outcome);
  return (
    <div className="op-overview">
      <div className="op-tile-row">
        <div className="op-tile">
          <span>Tokens / 14d</span>
          <strong>{totalTokens.toLocaleString()}</strong>
        </div>
        <div className="op-tile">
          <span>Turns / 14d</span>
          <strong>{totalTurns.toLocaleString()}</strong>
        </div>
        <div className="op-tile">
          <span>Latency avg / p95</span>
          <strong>
            {state.metrics.turns.avg_latency_ms}ms / {state.metrics.turns.p95_latency_ms}ms
          </strong>
        </div>
      </div>
      <Sparkline points={state.usage} />
      <h3>Queues</h3>
      <ul className="op-list" aria-label="Queue depths">
        {Object.entries(state.metrics.queues).map(([name, depth]) => (
          <li key={name}>
            <code>{name}</code>
            <span>{depth}</span>
          </li>
        ))}
      </ul>
      <h3>Turn outcomes / 24h</h3>
      <ul className="op-list" aria-label="Turn outcomes">
        {outcomes.length === 0 ? (
          <li>
            <span>No turns in the last 24 hours.</span>
          </li>
        ) : (
          outcomes.map(([outcome, count]) => (
            <li key={outcome}>
              <code>{outcome}</code>
              <span>{count}</span>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}
