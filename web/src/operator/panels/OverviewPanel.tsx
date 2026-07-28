import { useEffect, useState } from 'react';

import type { MetricsView, OperatorClient, UsagePoint } from '../api';
import { outcomeMeta } from '../labels';

type OverviewState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; usage: UsagePoint[]; metrics: MetricsView };

const CHART_WIDTH = 720;
const CHART_HEIGHT = 160;
const CHART_PAD = 8;

export function shortDay(day: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day.slice(5) : day;
}
function UsageChart({ points }: { points: UsagePoint[] }) {
  if (points.length === 0) {
    return <p className="panel-empty">暂无轮次记录。</p>;
  }
  const peak = Math.max(...points.map((point) => point.tokens), 1);
  const first = shortDay(points[0].day);
  const last = shortDay(points[points.length - 1].day);
  const coords = points.map((point, index) => {
    const x =
      points.length > 1
        ? CHART_PAD + (index * (CHART_WIDTH - CHART_PAD * 2)) / (points.length - 1)
        : CHART_WIDTH / 2;
    const y =
      CHART_HEIGHT -
      CHART_PAD -
      (point.tokens / peak) * (CHART_HEIGHT - CHART_PAD * 2 - 14);
    return [x, y] as const;
  });
  let line = `M ${coords[0][0].toFixed(1)} ${coords[0][1].toFixed(1)}`;
  for (let index = 1; index < coords.length; index += 1) {
    const [x0, y0] = coords[index - 1];
    const [x1, y1] = coords[index];
    const mid = ((x0 + x1) / 2).toFixed(1);
    line += ` C ${mid} ${y0.toFixed(1)}, ${mid} ${y1.toFixed(1)}, ${x1.toFixed(1)} ${y1.toFixed(1)}`;
  }
  const [lastX, lastY] = coords[coords.length - 1];
  const area = `${line} L ${lastX.toFixed(1)} ${CHART_HEIGHT} L ${coords[0][0].toFixed(1)} ${CHART_HEIGHT} Z`;
  return (
    <svg
      className="usage-chart"
      viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
      role="img"
      aria-label={`Token 用量趋势，${first} 至 ${last}，峰值 ${peak}`}
    >
      <defs>
        <linearGradient id="usage-gradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="rgba(0, 113, 227, 0.22)" />
          <stop offset="1" stopColor="rgba(0, 113, 227, 0)" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#usage-gradient)" />
      <path d={line} className="usage-line" fill="none" />
      <circle className="usage-dot" cx={lastX} cy={lastY} r="4" />
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
    return (
      <section className="card view-enter">
        <p className="panel-empty">正在加载用量数据…</p>
      </section>
    );
  }
  if (state.kind === 'error') {
    return (
      <section className="card view-enter">
        <p className="panel-error" role="alert">
          {state.message}
        </p>
      </section>
    );
  }

  const totalTokens = state.usage.reduce((sum, point) => sum + point.tokens, 0);
  const totalTurns = state.usage.reduce((sum, point) => sum + point.turns, 0);
  const outcomes = Object.entries(state.metrics.turns.by_outcome);
  const maxOutcome = Math.max(...outcomes.map(([, count]) => count), 1);
  const queues = Object.entries(state.metrics.queues);
  const willingness = state.metrics.willingness ?? { allowed: 0, blocked: 0 };

  return (
    <div className="overview-view view-enter">
      <div className="tile-row">
        <section className="card stat-tile">
          <small>Token 用量 · 14 天</small>
          <strong>{totalTokens.toLocaleString()}</strong>
        </section>
        <section className="card stat-tile">
          <small>对话轮次 · 14 天</small>
          <strong>{totalTurns.toLocaleString()}</strong>
        </section>
        <section className="card stat-tile">
          <small>延迟 平均 / P95</small>
          <strong>
            {state.metrics.turns.avg_latency_ms}
            <span className="stat-unit"> ms</span> / {state.metrics.turns.p95_latency_ms}
            <span className="stat-unit"> ms</span>
          </strong>
        </section>
        <section className="card stat-tile">
          <small>群聊意愿 · 24 小时</small>
          <strong>
            {willingness.allowed}
            <span className="stat-unit"> 进入 / </span>
            {willingness.blocked}
            <span className="stat-unit"> 拦截</span>
          </strong>
        </section>
      </div>
      <section className="card chart-card" aria-label="Token 用量趋势">
        <div className="card-heading">
          <h2>Token 用量趋势</h2>
          <small>最近 14 天 · 每日合计</small>
        </div>
        <UsageChart points={state.usage} />
        {state.usage.length > 0 && (
          <div className="chart-axis" aria-hidden="true">
            <small>{shortDay(state.usage[0].day)}</small>
            <small>{shortDay(state.usage[state.usage.length - 1].day)}</small>
          </div>
        )}
      </section>
      <div className="overview-split">
        <section className="card" aria-label="消息队列">
          <div className="card-heading">
            <h2>消息队列</h2>
          </div>
          {queues.length === 0 ? (
            <p className="panel-empty">暂无队列数据。</p>
          ) : (
            <ul className="queue-list">
              {queues.map(([name, depth]) => (
                <li key={name}>
                  <code>{name}</code>
                  <span className="queue-badge" data-active={depth > 0}>
                    {depth}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="card-note">积压深度，来自 Redis Streams 消费者组。</p>
        </section>
        <section className="card" aria-label="轮次结果">
          <div className="card-heading">
            <h2>轮次结果 · 24 小时</h2>
          </div>
          {outcomes.length === 0 ? (
            <p className="panel-empty">最近 24 小时没有轮次。</p>
          ) : (
            <ul className="outcome-list">
              {outcomes.map(([outcome, count]) => {
                const meta = outcomeMeta(outcome);
                return (
                  <li key={outcome}>
                    <div className="outcome-row">
                      <span>{meta.label}</span>
                      <strong>{count}</strong>
                    </div>
                    <div className="outcome-track" aria-hidden="true">
                      <div
                        className="outcome-bar"
                        data-tone={meta.tone}
                        style={{ width: `${Math.max((count / maxOutcome) * 100, 2)}%` }}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
