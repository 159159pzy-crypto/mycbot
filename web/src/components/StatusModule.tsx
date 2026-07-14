type StatusModuleProps = {
  stateName: 'loading' | 'ready' | 'degraded' | 'stale' | 'error';
  statusCode: string;
  statusMessage: string;
  statusDetail: string;
  checkedAt: string;
};

export function StatusModule({
  stateName,
  statusCode,
  statusMessage,
  statusDetail,
  checkedAt,
}: StatusModuleProps) {
  return (
    <article className="status-module" data-state={stateName}>
      <div className="module-heading">
        <span>Readiness beacon</span>
        <small>SYS / 001</small>
      </div>

      <div className="beacon-wrap" aria-hidden="true">
        <div className="beacon-ring beacon-ring--outer" />
        <div className="beacon-ring beacon-ring--inner" />
        <div className="beacon-core">
          <span>{statusCode}</span>
        </div>
        <span className="beacon-tick beacon-tick--top" />
        <span className="beacon-tick beacon-tick--right" />
        <span className="beacon-tick beacon-tick--bottom" />
        <span className="beacon-tick beacon-tick--left" />
      </div>

      <div className="status-copy">
        <p role="status" aria-live="polite" aria-atomic="true">
          {statusMessage}
        </p>
        <span>{statusDetail}</span>
      </div>

      <dl className="status-meta">
        <div>
          <dt>Last signal</dt>
          <dd>{checkedAt}</dd>
        </div>
        <div>
          <dt>Source</dt>
          <dd>API / READY</dd>
        </div>
      </dl>
    </article>
  );
}
