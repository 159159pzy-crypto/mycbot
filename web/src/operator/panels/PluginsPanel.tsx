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

  if (error) {
    return (
      <section className="card view-enter">
        <p className="panel-error" role="alert">
          {error}
        </p>
      </section>
    );
  }
  if (plugins === null || approved === null) {
    return (
      <section className="card view-enter">
        <p className="panel-empty">正在加载插件…</p>
      </section>
    );
  }

  return (
    <div className="plugin-view view-enter">
      <section className="card" aria-label="已注册插件">
        <div className="card-heading card-heading--stacked">
          <h2>已注册插件</h2>
          <small>通过内部代理接入，运行在隔离的 plugin-runner 进程中。</small>
        </div>
        {plugins.length === 0 ? (
          <p className="panel-empty">尚无插件注册到代理。</p>
        ) : (
          <div className="plugin-grid">
            {plugins.map((plugin) => (
              <div className="plugin-card" key={plugin.id}>
                <div className="plugin-name">
                  <strong>{plugin.id}</strong>
                  <code>v{plugin.version}</code>
                </div>
                <div className="plugin-rows">
                  <small>
                    <span>工具</span> {plugin.tools.join(', ') || '无'}
                  </small>
                  <small>
                    <span>钩子</span> {plugin.event_hooks.join(', ') || '无'}
                  </small>
                  <small>
                    <span>权限</span> {plugin.granted_capabilities.join(', ') || '无'}
                  </small>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="card" aria-label="已批准工具">
        <div className="card-heading card-heading--stacked">
          <h2>已批准工具</h2>
          <small>标记为需审批的工具，仅当其 ID 出现在此列表时才会运行。</small>
        </div>
        {approved.length === 0 ? (
          <p className="panel-empty">暂无长期批准。</p>
        ) : (
          <ul className="approval-list">
            {approved.map((toolId) => (
              <li key={toolId}>
                <code>{toolId}</code>
                <button
                  type="button"
                  className="pill-button pill-button--danger"
                  onClick={() => void save(approved.filter((item) => item !== toolId))}
                >
                  移除
                </button>
              </li>
            ))}
          </ul>
        )}
        <form
          className="approval-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (newToolId.trim()) {
              void save([...approved, newToolId.trim()]);
              setNewToolId('');
            }
          }}
        >
          <input
            value={newToolId}
            onChange={(event) => setNewToolId(event.target.value)}
            placeholder="输入工具 ID，如 get_weather"
            aria-label="批准工具 ID"
          />
          <button type="submit" className="pill-button pill-button--primary">
            批准
          </button>
        </form>
        <p className="panel-saved" role="status">
          {saved ? '批准列表已保存。' : ''}
        </p>
      </section>
    </div>
  );
}
