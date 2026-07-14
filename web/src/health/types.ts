export type DependencyStatus = {
  status: 'up' | 'down';
  detail?: string;
};

export type ReadinessPayload = {
  status: 'ready' | 'not_ready';
  dependencies: {
    database: DependencyStatus;
    redis: DependencyStatus;
  };
};

export type HealthSnapshot = {
  payload: ReadinessPayload;
  checkedAt: Date;
};

export type HealthState =
  | { kind: 'loading' }
  | { kind: 'resolved'; snapshot: HealthSnapshot; stale: boolean }
  | { kind: 'error' };

export type DependencyKey = keyof ReadinessPayload['dependencies'];
