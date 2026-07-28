import type { ReactNode } from 'react';

import {
  ChatIcon,
  DatabaseIcon,
  FlaskIcon,
  GridIcon,
  LockIcon,
  ModelIcon,
  PersonIcon,
  PlusBoxIcon,
  StatusIcon,
} from './icons';
import type { ReadinessView } from './readiness';
import { VIEWS, type ViewId } from './views';

const NAV_GROUPS: Array<{
  label: string;
  items: Array<{ id: ViewId; icon: ReactNode }>;
}> = [
  {
    label: '监控',
    items: [
      { id: 'status', icon: <StatusIcon /> },
      { id: 'overview', icon: <GridIcon /> },
      { id: 'operations', icon: <StatusIcon /> },
    ],
  },
  {
    label: '运营',
    items: [
      { id: 'conversations', icon: <ChatIcon /> },
      { id: 'sandbox', icon: <FlaskIcon /> },
      { id: 'memories', icon: <DatabaseIcon /> },
      { id: 'knowledge', icon: <DatabaseIcon /> },
    ],
  },
  {
    label: '配置',
    items: [
      { id: 'plugins', icon: <PlusBoxIcon /> },
      { id: 'models', icon: <ModelIcon /> },
      { id: 'persona', icon: <PersonIcon /> },
      { id: 'safety', icon: <LockIcon /> },
    ],
  },
];

export function Sidebar({
  view,
  readiness,
  onNavigate,
  onLock,
}: {
  view: ViewId;
  readiness: ReadinessView;
  onNavigate: (view: ViewId) => void;
  onLock: () => void;
}) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          MB
        </span>
        <span className="brand-copy">
          <strong>MyBot</strong>
          <small>控制平面</small>
        </span>
      </div>
      <nav className="sidebar-nav" aria-label="控制台导航">
        {NAV_GROUPS.map((group) => (
          <div className="nav-group" key={group.label}>
            <small className="nav-group-label">{group.label}</small>
            {group.items.map((item) => (
              <button
                key={item.id}
                type="button"
                className="nav-item"
                aria-current={view === item.id ? 'page' : undefined}
                onClick={() => onNavigate(item.id)}
              >
                {item.icon}
                {VIEWS[item.id].title}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div className="sidebar-foot">
        <div className="foot-status">
          <span
            className="status-dot"
            data-state={readiness.stateName}
            aria-hidden="true"
          />
          <span className="foot-status-text">{readiness.footStatus}</span>
          <span className="foot-status-time">{readiness.checkedAt}</span>
        </div>
        <button type="button" className="lock-button" onClick={onLock}>
          <LockIcon />
          锁定控制台
        </button>
      </div>
    </aside>
  );
}
