import { useEffect, useState } from 'react';

import type { OperatorClient, PluginView } from '../api';

export function PluginsPanel({ client }: { client: OperatorClient }) {
  const [plugins, setPlugins] = useState<PluginView[] | null>(null);
  const [approved, setApproved] = useState<string[] | null>(null);
  const [newToolId, setNewToolId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      client.get<{ runners: number; plugins: PluginView[] }>('/operator/plugins'),
      client.get<{ approved_ids: string[] }>('/operator/config/approvals'),
    ])
      .then(([health, approvals]) => {
        if (!cancelled) {
          setPlugins(health.plugins);
          setApproved(approvals.approved_ids);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  const save = async (next: string[]) => {
    try {
      const payload = await client.send<{ approved_ids: string[] }>(
        'PUT',
        '/operator/config/approvals',
        { approved_ids: next },
      );
      setApproved(payload.approved_ids);
      setSaved(true);
    } catch (cause) {
      setError(String(cause));
    }
  };

  if (error) return <p className="op-error">{error}</p>;
  if (plugins === null || approved === null) {
    return <p className="op-empty">Loading plugins…</p>;
  }

  return (
    <div>
      <h3>Registered plugins</h3>
      {plugins.length === 0 ? (
        <p className="op-empty">No plugins registered with the broker.</p>
      ) : (
        <ul className="op-list" aria-label="Plugins">
          {plugins.map((plugin) => (
            <li key={plugin.id}>
              <code>
                {plugin.id} v{plugin.version}
              </code>
              <span>
                tools: {plugin.tools.join(', ') || 'none'} · hooks:{' '}
                {plugin.event_hooks.join(', ') || 'none'} · grants:{' '}
                {plugin.granted_capabilities.join(', ') || 'none'}
              </span>
            </li>
          ))}
        </ul>
      )}
      <h3>Approved tools</h3>
      <p className="op-hint">
        Tools marked approval-required run only when their id is listed here.
      </p>
      <ul className="op-list" aria-label="Approved tools">
        {approved.length === 0 ? (
          <li>
            <span>No standing approvals.</span>
          </li>
        ) : (
          approved.map((toolId) => (
            <li key={toolId}>
              <code>{toolId}</code>
              <button
                type="button"
                onClick={() => void save(approved.filter((item) => item !== toolId))}
              >
                Remove
              </button>
            </li>
          ))
        )}
      </ul>
      <form
        className="op-inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (newToolId.trim()) {
            void save([...approved, newToolId.trim()]);
            setNewToolId('');
          }
        }}
      >
        <label>
          Approve tool id
          <input
            value={newToolId}
            onChange={(event) => setNewToolId(event.target.value)}
            placeholder="tool_id"
          />
        </label>
        <button type="submit">Approve</button>
      </form>
      {saved && <p className="op-hint">Approvals saved.</p>}
    </div>
  );
}
