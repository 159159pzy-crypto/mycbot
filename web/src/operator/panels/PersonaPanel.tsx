import { useEffect, useMemo, useState } from 'react';

import type {
  ConversationSummary,
  OperatorClient,
  PersonaVersion,
  ProfilesView,
  ProfileView,
  RelationshipView,
} from '../api';

type Draft = {
  id: string | null;
  name: string;
  description: string;
  systemPrompt: string;
  modelTier: string;
  tools: string;
  memoryEnabled: boolean;
  retrievalLimit: number;
  expressionExamples: number;
  relationshipEnabled: boolean;
  willingnessEnabled: boolean;
  threshold: number;
  sensitivity: number;
  keywords: string;
};

const emptyDraft = (): Draft => ({
  id: null,
  name: '新档位',
  description: '',
  systemPrompt: 'You are MyBot.',
  modelTier: 'default',
  tools: '',
  memoryEnabled: true,
  retrievalLimit: 5,
  expressionExamples: 3,
  relationshipEnabled: true,
  willingnessEnabled: false,
  threshold: 0.78,
  sensitivity: 1,
  keywords: '',
});

function toDraft(view: ProfileView): Draft {
  const profile = view.profile;
  return {
    id: profile.id,
    name: profile.name,
    description: profile.description,
    systemPrompt: view.persona.system_prompt,
    modelTier: profile.model_tier,
    tools: profile.tool_capabilities.join(', '),
    memoryEnabled: profile.memory.enabled,
    retrievalLimit: profile.memory.retrieval_limit,
    expressionExamples: profile.memory.expression_examples,
    relationshipEnabled: profile.memory.relationship_enabled,
    willingnessEnabled: profile.willingness.enabled,
    threshold: profile.willingness.threshold,
    sensitivity: profile.willingness.sensitivity,
    keywords: profile.willingness.keywords.join(', '),
  };
}

export function PersonaPanel({ client }: { client: OperatorClient }) {
  const [data, setData] = useState<ProfilesView | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [relationships, setRelationships] = useState<RelationshipView[]>([]);
  const [versions, setVersions] = useState<PersonaVersion[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [compareId, setCompareId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [note, setNote] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const [profiles, conversationData, relationshipData] = await Promise.all([
      client.get<ProfilesView>('/operator/profiles'),
      client.get<{ conversations: ConversationSummary[] }>('/operator/conversations?limit=200'),
      client.get<{ relationships: RelationshipView[] }>('/operator/relationships'),
    ]);
    setData(profiles);
    setConversations(conversationData.conversations);
    setRelationships(relationshipData.relationships);
    const retainedId = profiles.profiles.some((item) => item.profile.id === selectedId)
      ? selectedId
      : null;
    const nextId = retainedId ?? profiles.profiles[0]?.profile.id ?? null;
    setSelectedId(nextId);
    const selected = profiles.profiles.find((item) => item.profile.id === nextId);
    setDraft(selected ? toDraft(selected) : emptyDraft());
  };

  useEffect(() => {
    let cancelled = false;
    refresh().catch((cause: unknown) => {
      if (!cancelled) setError(String(cause));
    });
    return () => {
      cancelled = true;
    };
    // selectedId is intentionally retained across refreshes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  useEffect(() => {
    if (!selectedId) {
      setVersions([]);
      return;
    }
    client
      .get<{ versions: PersonaVersion[] }>(`/operator/profiles/${selectedId}/personas`)
      .then((result) => {
        setVersions(result.versions);
        setCompareId(result.versions[1]?.id ?? result.versions[0]?.id ?? null);
      })
      .catch((cause: unknown) => setError(String(cause)));
  }, [client, selectedId]);

  const selected = data?.profiles.find((item) => item.profile.id === selectedId) ?? null;
  const comparison = versions.find((version) => version.id === compareId) ?? null;
  const diff = useMemo(
    () => lineDiff(comparison?.system_prompt ?? '', draft.systemPrompt),
    [comparison, draft.systemPrompt],
  );

  const payload = () => ({
    name: draft.name,
    description: draft.description,
    model_tier: draft.modelTier,
    tool_capabilities: splitList(draft.tools),
    memory: {
      enabled: draft.memoryEnabled,
      retrieval_limit: draft.retrievalLimit,
      expression_examples: draft.expressionExamples,
      relationship_enabled: draft.relationshipEnabled,
    },
    willingness: {
      enabled: draft.willingnessEnabled,
      threshold: draft.threshold,
      sensitivity: draft.sensitivity,
      keywords: splitList(draft.keywords),
    },
  });

  const save = async () => {
    setStatus('正在保存…');
    setError(null);
    try {
      if (draft.id === null) {
        const created = await client.send<{ profile: ProfileView }>('POST', '/operator/profiles', {
          ...payload(),
          system_prompt: draft.systemPrompt,
        });
        setSelectedId(created.profile.profile.id);
      } else {
        await client.send('PUT', `/operator/profiles/${draft.id}`, payload());
        if (selected && draft.systemPrompt !== selected.persona.system_prompt) {
          await client.send('POST', `/operator/profiles/${draft.id}/personas`, {
            system_prompt: draft.systemPrompt,
            change_note: note || 'operator edit',
          });
        }
      }
      setNote('');
      await refresh();
      setStatus('已保存，后续回合自动使用新配置。');
    } catch (cause) {
      setStatus(null);
      setError(String(cause));
    }
  };

  const rollback = async (version: PersonaVersion) => {
    if (!selectedId) return;
    await client.send('POST', `/operator/profiles/${selectedId}/personas/rollback`, {
      target_version_id: version.id,
    });
    await refresh();
    const result = await client.get<{ versions: PersonaVersion[] }>(
      `/operator/profiles/${selectedId}/personas`,
    );
    setVersions(result.versions);
    setStatus(`已从 v${version.version} 创建新的回滚版本。`);
  };

  if (error && data === null) {
    return <section className="card panel-error" role="alert">{error}</section>;
  }
  if (data === null) {
    return <section className="card panel-empty">正在加载档位与人设…</section>;
  }

  return (
    <div className="profile-studio view-enter">
      <aside className="card profile-list-card">
        <div className="card-heading card-heading--stacked">
          <h2>会话档位</h2>
          <small>每个会话只绑定一个运行策略，未绑定时使用默认档位。</small>
        </div>
        <div className="profile-list" role="list">
          {data.profiles.map((item) => (
            <button
              type="button"
              className={item.profile.id === selectedId ? 'profile-row is-active' : 'profile-row'}
              key={item.profile.id}
              onClick={() => {
                setSelectedId(item.profile.id);
                setDraft(toDraft(item));
                setStatus(null);
              }}
            >
              <span>{item.profile.name}</span>
              <small>{item.profile.model_tier} · persona v{item.persona.version}</small>
            </button>
          ))}
        </div>
        <button
          type="button"
          className="pill-button pill-button--neutral"
          onClick={() => {
            setSelectedId(null);
            setDraft(emptyDraft());
            setVersions([]);
          }}
        >
          新建档位
        </button>
      </aside>

      <section className="card profile-editor-card">
        <div className="card-heading card-heading--stacked">
          <h2>{draft.id ? '档位设置' : '创建档位'}</h2>
          <small>模型、工具、记忆和插话意愿在下一回合生效，无需重启。</small>
        </div>
        <div className="profile-form-grid">
          <label>名称<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>模型档位<input value={draft.modelTier} onChange={(event) => setDraft({ ...draft, modelTier: event.target.value })} /></label>
          <label className="span-2">说明<input value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
          <label className="span-2">工具授权<input value={draft.tools} placeholder="web.search, web.fetch" onChange={(event) => setDraft({ ...draft, tools: event.target.value })} /></label>
        </div>

        <div className="policy-grid">
          <fieldset>
            <legend>记忆策略</legend>
            <Toggle label="启用记忆" checked={draft.memoryEnabled} onChange={(checked) => setDraft({ ...draft, memoryEnabled: checked })} />
            <Toggle label="注入关系" checked={draft.relationshipEnabled} onChange={(checked) => setDraft({ ...draft, relationshipEnabled: checked })} />
            <label>召回条数<input type="number" min={1} max={50} value={draft.retrievalLimit} onChange={(event) => setDraft({ ...draft, retrievalLimit: Number(event.target.value) })} /></label>
            <label>表达样例<input type="number" min={0} max={20} value={draft.expressionExamples} onChange={(event) => setDraft({ ...draft, expressionExamples: Number(event.target.value) })} /></label>
          </fieldset>
          <fieldset>
            <legend>群聊意愿</legend>
            <Toggle label="允许主动插话" checked={draft.willingnessEnabled} onChange={(checked) => setDraft({ ...draft, willingnessEnabled: checked })} />
            <label>阈值 <output>{draft.threshold.toFixed(2)}</output><input type="range" min="0" max="1" step="0.01" value={draft.threshold} onChange={(event) => setDraft({ ...draft, threshold: Number(event.target.value) })} /></label>
            <label>灵敏度 <output>{draft.sensitivity.toFixed(2)}</output><input type="range" min="0.5" max="1.5" step="0.05" value={draft.sensitivity} onChange={(event) => setDraft({ ...draft, sensitivity: Number(event.target.value) })} /></label>
            <label>关键词<input value={draft.keywords} placeholder="项目名, 群内称呼" onChange={(event) => setDraft({ ...draft, keywords: event.target.value })} /></label>
          </fieldset>
        </div>

        <label className="persona-prompt-label">当前人设<textarea rows={10} value={draft.systemPrompt} onChange={(event) => setDraft({ ...draft, systemPrompt: event.target.value })} /></label>
        {draft.id && <label className="change-note">版本说明<input value={note} placeholder="这次为什么修改" onChange={(event) => setNote(event.target.value)} /></label>}
        <div className="persona-actions">
          <button type="button" className="pill-button pill-button--primary" onClick={() => void save()}>保存</button>
          {draft.id && draft.name !== '默认' && (
            <button type="button" className="pill-button pill-button--danger" onClick={() => void client.send('DELETE', `/operator/profiles/${draft.id}`).then(refresh).catch((cause: unknown) => setError(String(cause)))}>删除</button>
          )}
          <span className="panel-saved" role="status">{status}</span>
        </div>
        {error && <p className="panel-error" role="alert">{error}</p>}
      </section>

      <section className="card persona-history-card">
        <div className="card-heading card-heading--stacked"><h2>不可变版本</h2><small>保存和回滚都会创建新版本，旧记录不会被覆盖。</small></div>
        {versions.length === 0 ? <p className="panel-empty">新档位保存后会显示版本历史。</p> : (
          <>
            <div className="version-strip">
              {versions.map((version) => (
                <button type="button" key={version.id} className={compareId === version.id ? 'version-chip is-active' : 'version-chip'} onClick={() => setCompareId(version.id)}>
                  v{version.version}<small>{version.change_note || '无说明'}</small>
                </button>
              ))}
            </div>
            <div className="persona-diff" aria-label="人设文本差异">
              {diff.map((line, index) => <div key={`${line.kind}-${index}`} data-kind={line.kind}><span>{line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : ' '}</span><code>{line.text || ' '}</code></div>)}
            </div>
            {comparison && comparison.id !== selected?.persona.id && <button type="button" className="pill-button pill-button--neutral" onClick={() => void rollback(comparison)}>回滚到 v{comparison.version}</button>}
          </>
        )}
      </section>

      <section className="card profile-bindings-card">
        <div className="card-heading card-heading--stacked"><h2>会话绑定</h2><small>精确控制客服群、闲聊群与私聊使用哪个档位。</small></div>
        <div className="binding-list">
          {conversations.map((conversation) => {
            const binding = data.bindings.find((item) => item.conversation_id === conversation.id);
            return <label key={conversation.id}><span><strong>{conversation.chat_id}</strong><small>{conversation.platform} · {conversation.chat_kind}</small></span><select value={binding?.profile_id ?? ''} onChange={(event) => void client.send('PUT', `/operator/conversations/${conversation.id}/profile`, { profile_id: event.target.value || null }).then(refresh)}><option value="">默认档位</option>{data.profiles.map((item) => <option key={item.profile.id} value={item.profile.id}>{item.profile.name}</option>)}</select></label>;
          })}
        </div>
      </section>

      <section className="card relationships-card">
        <div className="card-heading card-heading--stacked"><h2>关系</h2><small>熟悉度随互动增长并随记忆维护衰减；编辑同样创建新版本。</small></div>
        <div className="relationship-list">
          {relationships.length === 0 && <p className="panel-empty">尚无关系记录。</p>}
          {relationships.map((relationship) => <RelationshipEditor key={relationship.id} relationship={relationship} client={client} onSaved={refresh} />)}
        </div>
      </section>
    </div>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return <label className="toggle-row"><span>{label}</span><input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} /></label>;
}

function RelationshipEditor({ relationship, client, onSaved }: { relationship: RelationshipView; client: OperatorClient; onSaved: () => Promise<void> }) {
  const [impression, setImpression] = useState(relationship.impression);
  const [familiarity, setFamiliarity] = useState(relationship.familiarity);
  return <article className="relationship-row"><div><strong>{relationship.subject_identity_id}</strong><small>熟悉度 {familiarity.toFixed(1)} / 100</small></div><input value={impression} aria-label={`${relationship.subject_identity_id} 印象`} onChange={(event) => setImpression(event.target.value)} /><input type="range" min="0" max="100" step="0.5" value={familiarity} aria-label={`${relationship.subject_identity_id} 熟悉度`} onChange={(event) => setFamiliarity(Number(event.target.value))} /><button type="button" className="pill-button pill-button--neutral" onClick={() => void client.send('PUT', `/operator/relationships/${encodeURIComponent(relationship.subject_identity_id)}`, { impression, familiarity }).then(onSaved)}>保存</button></article>;
}

function splitList(value: string): string[] {
  return [...new Set(value.split(',').map((item) => item.trim()).filter(Boolean))];
}

function lineDiff(previous: string, current: string): Array<{ kind: 'same' | 'add' | 'remove'; text: string }> {
  const before = previous.split('\n');
  const after = current.split('\n');
  const result: Array<{ kind: 'same' | 'add' | 'remove'; text: string }> = [];
  const length = Math.max(before.length, after.length);
  for (let index = 0; index < length; index += 1) {
    if (before[index] === after[index]) result.push({ kind: 'same', text: before[index] ?? '' });
    else {
      if (before[index] !== undefined) result.push({ kind: 'remove', text: before[index] });
      if (after[index] !== undefined) result.push({ kind: 'add', text: after[index] });
    }
  }
  return result;
}
