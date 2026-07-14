import type { DependencyKey, HealthState } from '../health/types';

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

  const dependency = state.snapshot.payload.dependencies[key];
  const isUp = dependency.status === 'up';
  return {
    label: isUp ? 'Online' : 'Offline',
    detail: dependency.detail ?? (isUp ? 'Probe responding' : 'Probe unavailable'),
    state: isUp ? 'up' : 'down',
  };
}

export function DependencyRail({ health }: { health: HealthState }) {
  return (
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
          A degraded signal keeps the control plane visible while blocking readiness for
          traffic.
        </p>
      </div>
    </section>
  );
}
