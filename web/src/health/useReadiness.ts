import { useEffect, useState } from 'react';

import type { HealthSnapshot, HealthState, ReadinessPayload } from './types';

export const READINESS_POLL_INTERVAL_MS = 30_000;
export const READINESS_ERROR_RETRY_MS = 5_000;
export const READINESS_MAX_RETRY_MS = 60_000;
export const READINESS_FETCH_TIMEOUT_MS = 5_000;
export const READINESS_STALE_AFTER_MS = 60_000;

function isDependency(value: unknown): boolean {
  if (typeof value !== 'object' || value === null) return false;
  const status = Reflect.get(value, 'status');
  return status === 'up' || status === 'down';
}

function isReadinessPayload(value: unknown): value is ReadinessPayload {
  if (typeof value !== 'object' || value === null) return false;
  const status = Reflect.get(value, 'status');
  const dependencies = Reflect.get(value, 'dependencies');
  return (
    (status === 'ready' || status === 'not_ready') &&
    typeof dependencies === 'object' &&
    dependencies !== null &&
    isDependency(Reflect.get(dependencies, 'database')) &&
    isDependency(Reflect.get(dependencies, 'redis'))
  );
}

async function requestReadiness(controller: AbortController): Promise<ReadinessPayload> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timeoutId = setTimeout(() => {
      controller.abort();
      reject(new Error('Readiness request timed out'));
    }, READINESS_FETCH_TIMEOUT_MS);
  });

  try {
    const response = await Promise.race([
      fetch('/health/ready', {
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      }),
      timeout,
    ]);
    const payload: unknown = await response.json();
    if (!isReadinessPayload(payload)) {
      throw new Error('Invalid readiness response');
    }
    return payload;
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
}

export function useReadiness(): HealthState {
  const [health, setHealth] = useState<HealthState>({ kind: 'loading' });

  useEffect(() => {
    let disposed = false;
    let polling = false;
    let failures = 0;
    let latest: HealthSnapshot | undefined;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    let activeController: AbortController | undefined;

    const clearPollTimer = () => {
      if (pollTimer !== undefined) clearTimeout(pollTimer);
      pollTimer = undefined;
    };

    const clearStaleTimer = () => {
      if (staleTimer !== undefined) clearTimeout(staleTimer);
      staleTimer = undefined;
    };

    const markLatestStale = () => {
      if (disposed || latest === undefined) return;
      const expected = latest.checkedAt.getTime();
      setHealth((current) => {
        if (
          current.kind !== 'resolved' ||
          current.snapshot.checkedAt.getTime() !== expected
        ) {
          return current;
        }
        return { ...current, stale: true };
      });
    };

    const armStaleTimer = (snapshot: HealthSnapshot) => {
      clearStaleTimer();
      const remaining = Math.max(
        0,
        READINESS_STALE_AFTER_MS - (Date.now() - snapshot.checkedAt.getTime()),
      );
      staleTimer = setTimeout(markLatestStale, remaining);
    };

    const schedulePoll = (delay: number) => {
      clearPollTimer();
      if (disposed || document.visibilityState === 'hidden') return;
      pollTimer = setTimeout(() => void poll(), delay);
    };

    const poll = async () => {
      if (disposed || polling) return;
      polling = true;
      const controller = new AbortController();
      activeController = controller;
      try {
        const payload = await requestReadiness(controller);
        if (disposed) return;
        const snapshot = { payload, checkedAt: new Date() };
        latest = snapshot;
        failures = 0;
        setHealth({ kind: 'resolved', snapshot, stale: false });
        armStaleTimer(snapshot);
        schedulePoll(READINESS_POLL_INTERVAL_MS);
      } catch {
        if (disposed) return;
        failures += 1;
        if (latest === undefined) {
          setHealth({ kind: 'error' });
        } else {
          setHealth({ kind: 'resolved', snapshot: latest, stale: true });
        }
        const retryDelay = Math.min(
          READINESS_ERROR_RETRY_MS * 2 ** (failures - 1),
          READINESS_MAX_RETRY_MS,
        );
        schedulePoll(retryDelay);
      } finally {
        if (activeController === controller) activeController = undefined;
        polling = false;
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        clearPollTimer();
        return;
      }
      if (
        latest !== undefined &&
        Date.now() - latest.checkedAt.getTime() >= READINESS_STALE_AFTER_MS
      ) {
        markLatestStale();
      }
      void poll();
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    void poll();

    return () => {
      disposed = true;
      clearPollTimer();
      clearStaleTimer();
      activeController?.abort();
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, []);

  return health;
}
