import { useEffect, useState } from 'react';

import './App.css';

type DependencyStatus = {
  status: 'up' | 'down';
  detail?: string;
};

type ReadinessPayload = {
  status: 'ready' | 'not_ready';
  dependencies: {
    database: DependencyStatus;
    redis: DependencyStatus;
  };
};

type HealthState =
  | { kind: 'loading' }
  | { kind: 'resolved'; payload: ReadinessPayload; checkedAt: Date }
  | { kind: 'error' };

type DependencyKey = keyof ReadinessPayload['dependencies'];

const dependencyLabels: Record<DependencyKey, string> = {
  database: 'Database',
  redis: 'Redis',
};

const dependencyOrder: DependencyKey[] = ['database', 'redis'];

function dependencyView(state: HealthState, key: DependencyKey) {
  if (state.kind === 'loading') {
    return {
      label: 'Awaiting signal',
      detail: 'Probe handshake in progress',
      state: 'pending',
    };
  }

  if (state.kind === 'error') {
    return {
      label: 'Unknown',
      detail: 'No current health data',
      state: 'unknown',
    };
  }

  const dependency = state.payload.dependencies[key];
  const isUp = dependency.status === 'up';

  return {
    label: isUp ? 'Online' : 'Offline',
    detail: dependency.detail ?? (isUp ? 'Probe responding' : 'Probe unavailable'),
    state: isUp ? 'up' : 'down',
  };
}

function App() {
  const [health, setHealth] = useState<HealthState>({ kind: 'loading' });

  useEffect(() => {
    const controller = new AbortController();

    async function loadReadiness() {
      try {
        const response = await fetch('/health/ready', {
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        });
        const payload = (await response.json()) as ReadinessPayload;

        if (
          (payload.status !== 'ready' && payload.status !== 'not_ready') ||
          !payload.dependencies?.database ||
          !payload.dependencies.redis
        ) {
          throw new Error('Invalid readiness response');
        }

        setHealth({ kind: 'resolved', payload, checkedAt: new Date() });
      } catch (error) {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          setHealth({ kind: 'error' });
        }
      }
    }

    void loadReadiness();

    return () => controller.abort();
  }, []);

  const stateName =
    health.kind === 'loading'
      ? 'loading'
      : health.kind === 'error'
        ? 'error'
        : health.payload.status === 'ready'
          ? 'ready'
          : 'degraded';

  const statusCode =
    stateName === 'loading'
      ? 'LINKING'
      : stateName === 'ready'
        ? 'READY'
        : stateName === 'degraded'
          ? 'DEGRADED'
          : 'NO SIGNAL';

  const statusMessage =
    stateName === 'loading'
      ? 'Establishing telemetry link'
      : stateName === 'ready'
        ? 'All systems ready'
        : stateName === 'degraded'
          ? 'Readiness degraded'
          : 'Readiness signal unavailable';

  const checkedAt =
    health.kind === 'resolved'
      ? health.checkedAt.toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
      : 'Pending';

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
              A narrow signal deck for the services that keep the agent runtime
              available. No estimates. Only the latest probe response.
            </p>
          </div>

          <div className="hero-coordinate" aria-hidden="true">
            <span>NODE</span>
            <strong>CN-01</strong>
            <span>31.2304 N</span>
          </div>
        </section>

        <section className="readiness-grid" aria-label="Readiness overview">
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
              <span>
                {stateName === 'error'
                  ? 'The endpoint could not be reached. Current dependency state is unknown.'
                  : stateName === 'loading'
                    ? 'Waiting for the API to report its dependency probes.'
                    : 'Readiness reflects database and Redis probes from the API.'}
              </span>
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

          <section className="dependency-module" aria-labelledby="dependency-title">
            <div className="module-heading">
              <h2 id="dependency-title">Dependency rail</h2>
              <small>02 SIGNALS</small>
            </div>

            <ul className="dependency-list" aria-label="Dependency status">
              {dependencyOrder.map((key, index) => {
                const view = dependencyView(health, key);

                return (
                  <li
                    className="dependency-row"
                    data-state={view.state}
                    key={key}
                    aria-label={`${dependencyLabels[key]} dependency`}
                  >
                    <span className="dependency-number" aria-hidden="true">
                      0{index + 1}
                    </span>
                    <span className="dependency-glyph" aria-hidden="true" />
                    <span className="dependency-name">
                      <strong>{dependencyLabels[key]}</strong>
                      <small>{key === 'database' ? 'Persistent state' : 'Fast coordination'}</small>
                    </span>
                    <span className="dependency-reading">
                      <strong>{view.label}</strong>
                      <small>{view.detail}</small>
                    </span>
                  </li>
                );
              })}
            </ul>

            <div className="dependency-note">
              <span aria-hidden="true">-&gt;</span>
              <p>
                A degraded signal keeps the control plane visible while blocking
                readiness for traffic.
              </p>
            </div>
          </section>
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

export default App;
