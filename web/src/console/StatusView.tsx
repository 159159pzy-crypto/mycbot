import { DatabaseIcon, LayersIcon } from './icons';
import type { DependencyReading, ReadinessView } from './readiness';

function DependencyCard({
  label,
  name,
  role,
  tone,
  reading,
  icon,
}: {
  label: string;
  name: string;
  role: string;
  tone: 'blue' | 'orange';
  reading: DependencyReading;
  icon: React.ReactNode;
}) {
  return (
    <section className="card dep-card" aria-label={label}>
      <span className="dep-glyph" data-tone={tone} aria-hidden="true">
        {icon}
      </span>
      <div className="dep-name">
        <strong>{name}</strong>
        <small>
          {role} · {reading.detail}
        </small>
      </div>
      <span className="dep-pill" data-state={reading.state}>
        {reading.label}
      </span>
    </section>
  );
}

export function StatusView({ readiness }: { readiness: ReadinessView }) {
  return (
    <div className="status-view view-enter">
      <div className="status-grid">
        <section className="card status-hero" aria-label="就绪状态">
          <div className="status-ring" data-state={readiness.stateName} aria-hidden="true">
            <div className="status-ring-core">
              <strong data-state={readiness.stateName}>{readiness.word}</strong>
            </div>
          </div>
          <div className="status-copy">
            <h2 className="status-title">{readiness.title}</h2>
            <p>{readiness.detail}</p>
            <div className="status-chips">
              <span className="meta-chip">
                数据来源 <code>API · READY</code>
              </span>
              <span className="meta-chip">
                端点 <code>GET /health/ready</code>
              </span>
            </div>
          </div>
        </section>
        <div className="dep-stack">
          <DependencyCard
            label="数据库依赖"
            name="PostgreSQL 数据库"
            role="持久化状态"
            tone="blue"
            reading={readiness.database}
            icon={<DatabaseIcon size={20} />}
          />
          <DependencyCard
            label="Redis 依赖"
            name="Redis"
            role="快速协调"
            tone="orange"
            reading={readiness.redis}
            icon={<LayersIcon size={20} />}
          />
        </div>
      </div>
      <section className="card contract-strip" aria-label="探测契约">
        <div>
          <small>探测端点</small>
          <code>GET /health/ready</code>
        </div>
        <div>
          <small>探测契约</small>
          <span>数据库 + Redis</span>
        </div>
        <div>
          <small>失败策略</small>
          <span>故障即拒绝（fail closed）</span>
        </div>
      </section>
      <p className="view-footnote">
        依赖降级时控制平面保持可见，但就绪探测会拒绝新流量。此页不做估算，仅展示最近一次探测响应。
      </p>
    </div>
  );
}
