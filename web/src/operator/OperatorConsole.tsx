import { useMemo, useState } from 'react';

import { createOperatorClient } from './api';
import { ConversationsPanel } from './panels/ConversationsPanel';
import { MemoriesPanel } from './panels/MemoriesPanel';
import { OverviewPanel } from './panels/OverviewPanel';
import { PersonaPanel } from './panels/PersonaPanel';
import { PluginsPanel } from './panels/PluginsPanel';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'conversations', label: 'Conversations' },
  { id: 'memories', label: 'Memories' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'persona', label: 'Persona' },
] as const;

type TabId = (typeof TABS)[number]['id'];

export function OperatorConsole() {
  const [token, setToken] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>('overview');

  const client = useMemo(() => {
    if (token === null) return null;
    return createOperatorClient(token, () => {
      setToken(null);
      setNotice('The operator token was rejected. Enter it again.');
    });
  }, [token]);

  if (client === null) {
    return (
      <section className="operator-console" aria-labelledby="operator-title">
        <div className="module-heading">
          <span id="operator-title">Operator console</span>
          <small>OPS / 002</small>
        </div>
        <form
          className="op-token-gate"
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim()) {
              setToken(draft.trim());
              setDraft('');
              setNotice(null);
            }
          }}
        >
          <p className="op-hint">
            Enter the operator token (MYBOT_OPERATOR_TOKEN). It is kept in memory only
            and never stored in the browser.
          </p>
          <label>
            Operator token
            <input
              type="password"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              autoComplete="off"
            />
          </label>
          <button type="submit">Unlock console</button>
          {notice && <p className="op-error">{notice}</p>}
        </form>
      </section>
    );
  }

  return (
    <section className="operator-console" aria-labelledby="operator-title">
      <div className="module-heading">
        <span id="operator-title">Operator console</span>
        <small>OPS / 002</small>
      </div>
      <nav className="op-tabs" aria-label="Console sections">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            aria-pressed={tab === entry.id}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
        <button
          type="button"
          className="op-lock"
          onClick={() => {
            setToken(null);
            setNotice(null);
          }}
        >
          Lock
        </button>
      </nav>
      <div className="op-panel">
        {tab === 'overview' && <OverviewPanel client={client} />}
        {tab === 'conversations' && <ConversationsPanel client={client} />}
        {tab === 'memories' && <MemoriesPanel client={client} />}
        {tab === 'plugins' && <PluginsPanel client={client} />}
        {tab === 'persona' && <PersonaPanel client={client} />}
      </div>
    </section>
  );
}
