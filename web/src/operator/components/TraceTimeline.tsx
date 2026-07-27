import type { TraceSpanView } from '../api';

const STATUS_LABELS: Record<TraceSpanView['status'], string> = {
  ok: '完成',
  error: '失败',
  skipped: '跳过',
};

export function TraceTimeline({
  traces,
  title = '链路追踪',
}: {
  traces: TraceSpanView[];
  title?: string;
}) {
  return (
    <section className="card trace-card" aria-label={title}>
      <div className="card-heading">
        <h2>{title}</h2>
        <small>{traces.length} 个阶段</small>
      </div>
      {traces.length === 0 ? (
        <p className="panel-empty">暂无追踪数据。</p>
      ) : (
        <ol className="trace-timeline" aria-label="Trace spans">
          {traces.map((span) => (
            <li key={span.id} data-status={span.status}>
              <span className="trace-marker" aria-hidden="true" />
              <div className="trace-content">
                <div className="trace-heading">
                  <code>{span.stage}</code>
                  <span className="tone-chip" data-tone={span.status === 'ok' ? 'green' : span.status === 'error' ? 'red' : 'gray'}>
                    {STATUS_LABELS[span.status]}
                  </span>
                  <strong>{span.duration_ms} ms</strong>
                </div>
                {Object.keys(span.attributes).length > 0 && (
                  <details className="trace-attributes">
                    <summary>查看阶段属性</summary>
                    <pre>{JSON.stringify(span.attributes, null, 2)}</pre>
                  </details>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
