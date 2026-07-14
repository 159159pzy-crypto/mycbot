import type { HealthState } from '../health/types';
import { DependencyRail } from './DependencyRail';
import { StatusModule } from './StatusModule';

function viewState(health: HealthState) {
  if (health.kind === 'loading') {
    return {
      stateName: 'loading' as const,
      statusCode: 'LINKING',
      statusMessage: 'Establishing telemetry link',
      statusDetail: 'Waiting for the API to report its dependency probes.',
      checkedAt: 'Pending',
    };
  }
  if (health.kind === 'error') {
    return {
      stateName: 'error' as const,
      statusCode: 'NO SIGNAL',
      statusMessage: 'Readiness signal unavailable',
      statusDetail: 'The endpoint could not be reached. Current dependency state is unknown.',
      checkedAt: 'Pending',
    };
  }

  const checkedAt = health.snapshot.checkedAt.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  if (health.stale) {
    return {
      stateName: 'stale' as const,
      statusCode: 'STALE',
      statusMessage: 'Readiness data stale',
      statusDetail: 'Showing the last dependency signal while a fresh probe is unavailable.',
      checkedAt,
    };
  }
  if (health.snapshot.payload.status === 'ready') {
    return {
      stateName: 'ready' as const,
      statusCode: 'READY',
      statusMessage: 'All systems ready',
      statusDetail: 'Readiness reflects database and Redis probes from the API.',
      checkedAt,
    };
  }
  return {
    stateName: 'degraded' as const,
    statusCode: 'DEGRADED',
    statusMessage: 'Readiness degraded',
    statusDetail: 'Readiness reflects database and Redis probes from the API.',
    checkedAt,
  };
}

export function ReadinessConsole({ health }: { health: HealthState }) {
  const view = viewState(health);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to readiness overview
      </a>

      <header className="topbar">
        <div className="brand-lockup" aria-label="MyBot control plane">
          <span className="brand-mark" aria-hidden="true">
            MB
          </span>
          <span>
            <strong>MYBOT</strong>
            <small>Control plane</small>
          </span>
        </div>
        <div className="channel-tag">
          <span>Channel</span>
          <strong>01 / FOUNDATION</strong>
        </div>
        <div className="route-tag">
          <span>Probe route</span>
          <code>/health/ready</code>
        </div>
      </header>

      <main id="main-content">
        <section className="hero" aria-labelledby="page-title">
          <div className="hero-index" aria-hidden="true">
            <span>01</span>
            <i />
            <small>OPS</small>
          </div>
          <div className="hero-copy">
            <p className="eyebrow">System foundation / live dependency brief</p>
            <h1 id="page-title">
              Operations <span>readiness</span>
            </h1>
            <p className="lede">
              A narrow signal deck for the services that keep the agent runtime available. No
              estimates. Only the latest probe response.
            </p>
          </div>
          <div className="hero-coordinate" aria-hidden="true">
            <span>NODE</span>
            <strong>CN-01</strong>
            <span>31.2304 N</span>
          </div>
        </section>

        <section className="readiness-grid" aria-label="Readiness overview">
          <StatusModule {...view} />
          <DependencyRail health={health} />
        </section>

        <section className="signal-strip" aria-label="Probe context">
          <div>
            <span>Endpoint</span>
            <code>GET /health/ready</code>
          </div>
          <div>
            <span>Contract</span>
            <strong>Database + Redis</strong>
          </div>
          <div>
            <span>Policy</span>
            <strong>Fail closed</strong>
          </div>
          <div className="signal-bars" aria-hidden="true">
            {Array.from({ length: 12 }, (_, index) => (
              <i key={index} />
            ))}
          </div>
        </section>
      </main>

      <footer>
        <span>MYBOT / MILESTONE 01</span>
        <span>OPERATOR SURFACE / READ ONLY</span>
      </footer>
    </div>
  );
}
