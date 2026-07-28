import { useEffect, useMemo, useState } from 'react';

import type {
  OperatorClient,
  PairingPolicyView,
  PairingRequestView,
  PluginView,
  RegistryPluginView,
  SkillView,
} from '../api';

type PluginHealth = {
  runners: number;
  plugins: PluginView[];
  services?: Array<{ name: string; version: number; capability: string }>;
  load_errors?: Array<{ plugin_id: string; detail: string }>;
};

export function PluginsPanel({ client }: { client: OperatorClient }) {
  const [health, setHealth] = useState<PluginHealth | null>(null);
  const [approved, setApproved] = useState<string[] | null>(null);
  const [skills, setSkills] = useState<SkillView[]>([]);
  const [registry, setRegistry] = useState<RegistryPluginView[]>([]);
  const [policies, setPolicies] = useState<PairingPolicyView[]>([]);
  const [requests, setRequests] = useState<PairingRequestView[]>([]);
  const [newToolId, setNewToolId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState('');

  const load = async () => {
    const [pluginsPayload, approvals, skillsPayload, registryPayload, pairingPayload] =
      await Promise.all([
        client.get<PluginHealth>('/operator/plugins'),
        client.get<{ approved_ids: string[] }>('/operator/config/approvals'),
        client.get<{ skills: SkillView[] }>('/operator/skills').catch(() => ({ skills: [] })),
        client
          .get<{ plugins: RegistryPluginView[] }>('/operator/plugins/registry')
          .catch(() => ({ plugins: [] })),
        client
          .get<{ policies: PairingPolicyView[]; requests: PairingRequestView[] }>(
            '/operator/pairing',
          )
          .catch(() => ({ policies: [], requests: [] })),
      ]);
    setHealth(pluginsPayload);
    setApproved(approvals.approved_ids);
    setSkills(skillsPayload.skills);
    setRegistry(registryPayload.plugins);
    setPolicies(pairingPayload.policies);
    setRequests(pairingPayload.requests);
  };

  useEffect(() => {
    let cancelled = false;
    void load().catch((cause: unknown) => {
      if (!cancelled) setError(String(cause));
    });
    return () => {
      cancelled = true;
    };
  }, [client]);

  const saveApprovals = async (next: string[]) => {
    try {
      const payload = await client.send<{ approved_ids: string[] }>(
        'PUT',
        '/operator/config/approvals',
        { approved_ids: next },
      );
      setApproved(payload.approved_ids);
      setSaved('工具批准列表已保存。');
    } catch (cause) {
      setError(String(cause));
    }
  };

  if (error) {
    return (
      <section className="card view-enter">
        <p className="panel-error" role="alert">{error}</p>
        <button type="button" className="pill-button" onClick={() => setError(null)}>返回</button>
      </section>
    );
  }
  if (health === null || approved === null) {
    return <section className="card view-enter"><p className="panel-empty">正在加载扩展系统…</p></section>;
  }

  return (
    <div className="plugin-view view-enter">
      <section className="card" aria-label="已注册插件">
        <div className="card-heading card-heading--stacked">
          <h2>插件运行时</h2>
          <small>每个插件独占子进程；重载、崩溃退避和熔断不会影响其他插件。</small>
        </div>
        {health.plugins.length === 0 ? (
          <p className="panel-empty">尚无插件注册到 broker。</p>
        ) : (
          <div className="plugin-grid">
            {health.plugins.map((plugin) => (
              <PluginCard
                key={plugin.id}
                plugin={plugin}
                client={client}
                onChanged={load}
                onError={(cause) => setError(String(cause))}
              />
            ))}
          </div>
        )}
        {health.load_errors?.map((item) => (
          <p className="panel-error" key={item.plugin_id}>{item.plugin_id}: {item.detail}</p>
        ))}
        <div className="service-strip">
          {(health.services ?? []).map((service) => (
            <code key={service.name}>{service.name}@{service.version}</code>
          ))}
        </div>
      </section>

      <section className="card" aria-label="Markdown 技能">
        <div className="card-heading card-heading--stacked">
          <h2>Markdown 技能</h2>
          <small>目录只进入提示词；完整正文仅在模型调用 load_skill 时加载。</small>
        </div>
        <div className="skill-list">
          {skills.map((skill) => (
            <SkillEditor
              key={skill.name}
              skill={skill}
              client={client}
              onSaved={(next) => setSkills((items) => items.map((item) => item.name === next.name ? next : item))}
              onError={(cause) => setError(String(cause))}
            />
          ))}
        </div>
      </section>

      <section className="card" aria-label="可信插件索引">
        <div className="card-heading card-heading--stacked">
          <h2>可信索引</h2>
          <small>安装顺序：HTTPS 下载、压缩包检查、manifest 哈希比对、版本目录落盘、supervisor 加载。</small>
        </div>
        {registry.length === 0 ? <p className="panel-empty">registry.json 当前为空。</p> : (
          <ul className="approval-list">
            {registry.map((entry) => (
              <li key={`${entry.name}@${entry.version}`}>
                <span><code>{entry.name}</code> <small>v{entry.version}</small></span>
                <button type="button" className="pill-button pill-button--primary" onClick={() => {
                  void client.send('POST', '/operator/plugins/install', { name: entry.name })
                    .then(load)
                    .catch((cause) => setError(String(cause)));
                }}>安装</button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card" aria-label="私聊配对">
        <div className="card-heading card-heading--stacked">
          <h2>陌生私聊配对</h2>
          <small>paired 模式下未知用户不会创建会话、写入消息或调用模型。</small>
        </div>
        <div className="pairing-policies">
          {policies.map((policy) => (
            <label key={`${policy.platform}:${policy.connection_id}`}>
              <span>{policy.platform.toUpperCase()} · {policy.connection_id}</span>
              <select value={policy.policy} onChange={(event) => {
                const next = event.target.value as PairingPolicyView['policy'];
                void client.send<PairingPolicyView>('PUT', `/operator/pairing/policies/${policy.platform}`, {
                  connection_id: policy.connection_id,
                  policy: next,
                  allowlist: policy.allowlist,
                }).then((savedPolicy) => setPolicies((items) => items.map((item) => item.platform === savedPolicy.platform ? savedPolicy : item)))
                  .catch((cause) => setError(String(cause)));
              }}>
                <option value="open">Open</option>
                <option value="paired">Paired</option>
                <option value="allowlist">Allowlist</option>
              </select>
            </label>
          ))}
        </div>
        {requests.length === 0 ? <p className="panel-empty">没有待审批请求。</p> : (
          <ul className="pairing-requests">
            {requests.map((request) => (
              <li key={request.id}>
                <span><strong>{request.subject_identity_id}</strong><code>{request.code}</code></span>
                <div>
                  <button type="button" className="pill-button pill-button--primary" onClick={() => {
                    void client.send('POST', `/operator/pairing/requests/${request.id}/approve`).then(load).catch((cause) => setError(String(cause)));
                  }}>批准</button>
                  <button type="button" className="pill-button" onClick={() => {
                    void client.send('POST', `/operator/pairing/requests/${request.id}/dismiss`).then(load).catch((cause) => setError(String(cause)));
                  }}>忽略</button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card" aria-label="已批准工具">
        <div className="card-heading card-heading--stacked">
          <h2>高风险工具批准</h2>
          <small>仅当工具 ID 在此列表中时，approval_required 工具才会运行。</small>
        </div>
        <ul className="approval-list">
          {approved.map((toolId) => (
            <li key={toolId}><code>{toolId}</code><button type="button" className="pill-button pill-button--danger" onClick={() => void saveApprovals(approved.filter((item) => item !== toolId))}>移除</button></li>
          ))}
        </ul>
        <form className="approval-form" onSubmit={(event) => {
          event.preventDefault();
          if (newToolId.trim()) {
            void saveApprovals([...approved, newToolId.trim()]);
            setNewToolId('');
          }
        }}>
          <input value={newToolId} onChange={(event) => setNewToolId(event.target.value)} placeholder="输入工具 ID" aria-label="批准工具 ID" />
          <button type="submit" className="pill-button pill-button--primary">批准</button>
        </form>
        <p className="panel-saved" role="status">{saved}</p>
      </section>
    </div>
  );
}

function PluginCard({ plugin, client, onChanged, onError }: {
  plugin: PluginView;
  client: OperatorClient;
  onChanged: () => Promise<void>;
  onError: (cause: unknown) => void;
}) {
  const initial = useMemo(() => plugin.config ?? {}, [plugin.config]);
  const [config, setConfig] = useState<Record<string, unknown>>(initial);
  const properties = ((plugin.config_schema?.properties ?? {}) as Record<string, { type?: string }>);
  return (
    <article className="plugin-card">
      <div className="plugin-name"><strong>{plugin.id}</strong><code>v{plugin.version}</code><span className={`status-dot status-dot--${plugin.state ?? 'running'}`}>{plugin.state ?? 'running'}</span></div>
      <div className="plugin-rows">
        <small><span>工具</span> {plugin.tools.join(', ') || '无'}</small>
        <small><span>任务</span> {(plugin.tasks ?? []).map((task) => `${task.id}/${task.interval_seconds}s`).join(', ') || '无'}</small>
        <small><span>服务</span> {Object.entries(plugin.requires ?? {}).map(([name, version]) => `${name}@${version}`).join(', ') || '无'}</small>
        {plugin.last_error ? <small className="panel-error">{plugin.last_error}</small> : null}
      </div>
      {Object.keys(properties).length > 0 ? <div className="schema-form">
        {Object.entries(properties).map(([name, schema]) => (
          <label key={name}><span>{name}</span>{schema.type === 'boolean' ? (
            <input type="checkbox" checked={Boolean(config[name])} onChange={(event) => setConfig({ ...config, [name]: event.target.checked })} />
          ) : (
            <input type={schema.type === 'integer' || schema.type === 'number' ? 'number' : 'text'} value={String(config[name] ?? '')} onChange={(event) => setConfig({ ...config, [name]: schema.type === 'integer' || schema.type === 'number' ? Number(event.target.value) : event.target.value })} />
          )}</label>
        ))}
        <button type="button" className="pill-button" onClick={() => void client.send('PUT', `/operator/plugins/${plugin.id}/config`, { config }).then(onChanged).catch(onError)}>保存配置</button>
      </div> : null}
      <div className="plugin-actions">
        <button type="button" className="pill-button" onClick={() => void client.send('POST', `/operator/plugins/${plugin.id}/action`, { action: plugin.enabled === false ? 'enable' : 'disable' }).then(onChanged).catch(onError)}>{plugin.enabled === false ? '启用' : '停用'}</button>
        <button type="button" className="pill-button pill-button--primary" onClick={() => void client.send('POST', `/operator/plugins/${plugin.id}/action`, { action: 'reload' }).then(onChanged).catch(onError)}>重载</button>
      </div>
    </article>
  );
}

function SkillEditor({ skill, client, onSaved, onError }: {
  skill: SkillView;
  client: OperatorClient;
  onSaved: (skill: SkillView) => void;
  onError: (cause: unknown) => void;
}) {
  const [content, setContent] = useState(skill.content);
  return <article className="skill-editor">
    <div><strong>{skill.name}</strong><small>{skill.description}</small></div>
    <textarea aria-label={`${skill.name} 技能正文`} value={content} onChange={(event) => setContent(event.target.value)} />
    <div className="plugin-actions">
      <button type="button" className="pill-button" onClick={() => void client.send<{ skill: SkillView }>('PUT', `/operator/skills/${skill.name}/enabled`, { enabled: !skill.enabled }).then((payload) => onSaved(payload.skill)).catch(onError)}>{skill.enabled ? '停用' : '启用'}</button>
      <button type="button" className="pill-button pill-button--primary" onClick={() => void client.send<{ skill: SkillView }>('PUT', `/operator/skills/${skill.name}`, { content }).then((payload) => onSaved(payload.skill)).catch(onError)}>保存</button>
    </div>
  </article>;
}
