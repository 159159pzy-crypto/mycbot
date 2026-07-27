import { useMemo, useState } from 'react';

import type { HealthState } from '../health/types';
import { createOperatorClient } from '../operator/api';
import { ConversationsPanel } from '../operator/panels/ConversationsPanel';
import { MemoriesPanel } from '../operator/panels/MemoriesPanel';
import { ModelsPanel } from '../operator/panels/ModelsPanel';
import { OverviewPanel } from '../operator/panels/OverviewPanel';
import { PersonaPanel } from '../operator/panels/PersonaPanel';
import { PluginsPanel } from '../operator/panels/PluginsPanel';
import { SandboxPanel } from '../operator/panels/SandboxPanel';
import { LockScreen } from './LockScreen';
import { readinessView } from './readiness';
import { Sidebar } from './Sidebar';
import { StatusView } from './StatusView';
import { VIEWS, type ViewId } from './views';

export function Console({ health }: { health: HealthState }) {
  const [token, setToken] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [view, setView] = useState<ViewId>('status');

  const client = useMemo(() => {
    if (token === null) return null;
    return createOperatorClient(token, () => {
      setToken(null);
      setNotice('操作员令牌被拒绝，请重新输入。');
    });
  }, [token]);

  if (client === null) {
    return (
      <LockScreen
        notice={notice}
        onUnlock={(next) => {
          setToken(next);
          setNotice(null);
        }}
      />
    );
  }

  const readiness = readinessView(health);

  return (
    <div className="console">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <p className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
        就绪状态：{readiness.title}
      </p>
      <Sidebar
        view={view}
        readiness={readiness}
        onNavigate={setView}
        onLock={() => {
          setToken(null);
          setNotice(null);
        }}
      />
      <div className="console-body">
        <header className="console-header">
          <div className="console-heading">
            <h1>{VIEWS[view].title}</h1>
            <p>{VIEWS[view].subtitle}</p>
          </div>
          <div className="signal-pill" title={readiness.footStatus}>
            <span
              className="status-dot"
              data-state={readiness.stateName}
              aria-hidden="true"
            />
            <span className="visually-hidden">{readiness.footStatus}</span>
            <span>最近信号</span>
            <strong>{readiness.checkedAt}</strong>
          </div>
        </header>
        <main id="main-content" className="console-main">
          {view === 'status' && <StatusView readiness={readiness} />}
          {view === 'overview' && <OverviewPanel client={client} />}
          {view === 'conversations' && <ConversationsPanel client={client} />}
          {view === 'sandbox' && <SandboxPanel client={client} />}
          {view === 'memories' && <MemoriesPanel client={client} />}
          {view === 'plugins' && <PluginsPanel client={client} />}
          {view === 'models' && <ModelsPanel client={client} />}
          {view === 'persona' && <PersonaPanel client={client} />}
        </main>
      </div>
    </div>
  );
}
