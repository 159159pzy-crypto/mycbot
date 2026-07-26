import type { HealthState } from '../health/types';

export type StatusName = 'ready' | 'degraded' | 'stale' | 'loading' | 'error';

export type DependencyState = 'up' | 'down' | 'pending' | 'unknown';

export type DependencyReading = {
  state: DependencyState;
  label: string;
  detail: string;
};

export type ReadinessView = {
  stateName: StatusName;
  word: string;
  title: string;
  detail: string;
  footStatus: string;
  checkedAt: string;
  database: DependencyReading;
  redis: DependencyReading;
};

const DEPENDENCY_LABEL: Record<DependencyState, string> = {
  up: '在线',
  down: '离线',
  pending: '等待信号',
  unknown: '未知',
};

const DEPENDENCY_DETAIL: Record<DependencyState, string> = {
  up: '探测响应正常',
  down: '探测无响应',
  pending: '握手进行中',
  unknown: '暂无健康数据',
};

function reading(state: DependencyState, detail?: string): DependencyReading {
  return {
    state,
    label: DEPENDENCY_LABEL[state],
    detail: detail ?? DEPENDENCY_DETAIL[state],
  };
}

export function readinessView(health: HealthState): ReadinessView {
  if (health.kind === 'loading') {
    return {
      stateName: 'loading',
      word: '连接',
      title: '正在建立遥测链路',
      detail: '等待 API 返回依赖探测结果。',
      footStatus: '正在连接',
      checkedAt: '—',
      database: reading('pending'),
      redis: reading('pending'),
    };
  }
  if (health.kind === 'error') {
    return {
      stateName: 'error',
      word: '无信号',
      title: '无法获取就绪信号',
      detail: '探测端点无法访问，当前依赖状态未知。',
      footStatus: '信号丢失',
      checkedAt: '—',
      database: reading('unknown'),
      redis: reading('unknown'),
    };
  }

  const { payload, checkedAt } = health.snapshot;
  const formatted = checkedAt.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  const database = reading(
    payload.dependencies.database.status,
    payload.dependencies.database.detail,
  );
  const redis = reading(
    payload.dependencies.redis.status,
    payload.dependencies.redis.detail,
  );

  if (health.stale) {
    return {
      stateName: 'stale',
      word: '过期',
      title: '就绪数据已过期',
      detail: '暂时无法获取新探测，当前展示最后一次依赖信号。',
      footStatus: '数据过期',
      checkedAt: formatted,
      database,
      redis,
    };
  }
  if (payload.status === 'ready') {
    return {
      stateName: 'ready',
      word: '正常',
      title: '一切正常',
      detail: '就绪状态实时反映 API 对数据库与 Redis 的探测结果，当前所有依赖均在线。',
      footStatus: '系统正常',
      checkedAt: formatted,
      database,
      redis,
    };
  }
  return {
    stateName: 'degraded',
    word: '降级',
    title: '服务已降级',
    detail: '部分依赖探测失败。控制平面保持可见，就绪探测已拒绝新流量。',
    footStatus: '服务降级',
    checkedAt: formatted,
    database,
    redis,
  };
}
