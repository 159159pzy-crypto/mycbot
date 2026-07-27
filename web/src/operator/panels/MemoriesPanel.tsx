import { useCallback, useEffect, useRef, useState } from 'react';

import type {
  CoreBlockView,
  MemoryOperationView,
  MemoryView,
  OperatorClient,
} from '../api';
import { memoryKindLabel, privacyLabel, scopeLabel } from '../labels';

const SCOPE_FILTERS = [
  { id: '', label: '全部' },
  { id: 'SUBJECT', label: '主体' },
  { id: 'CONVERSATION', label: '会话' },
  { id: 'GLOBAL', label: '全局' },
] as const;

type CoreDraft = {
  label: 'persona' | 'user_profile';
  subject_identity_id: string;
  content: string;
  token_budget: number;
};

const EMPTY_CORE: CoreDraft = {
  label: 'persona',
  subject_identity_id: '',
  content: '',
  token_budget: 800,
};

function memoryState(memory: MemoryView): MemoryView['state'] {
  return memory.state ?? (memory.revoked_at ? 'revoked' : memory.invalid_at ? 'invalidated' : 'active');
}

export function MemoriesPanel({ client }: { client: OperatorClient }) {
  const [memories, setMemories] = useState<MemoryView[] | null>(null);
  const [coreBlocks, setCoreBlocks] = useState<CoreBlockView[]>([]);
  const [coreDraft, setCoreDraft] = useState<CoreDraft>(EMPTY_CORE);
  const [scope, setScope] = useState<string>('');
  const [includeHistory, setIncludeHistory] = useState(false);
  const [history, setHistory] = useState<MemoryView[] | null>(null);
  const [operations, setOperations] = useState<MemoryOperationView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const params = new URLSearchParams({ state: includeHistory ? 'all' : 'active' });
      if (scope) params.set('scope', scope);
      const payload = await client.get<{ memories: MemoryView[] }>(
        `/operator/memories?${params.toString()}`,
      );
      if (loadSeq.current !== seq) return;
      setMemories(payload.memories);
      setError(null);
    } catch (cause) {
      if (loadSeq.current !== seq) return;
      setError(String(cause));
    }
  }, [client, scope, includeHistory]);

  const loadCore = useCallback(async () => {
    try {
      const payload = await client.get<{ core_blocks: CoreBlockView[] }>('/operator/core-blocks');
      setCoreBlocks(payload.core_blocks);
    } catch {
      setCoreBlocks([]);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void loadCore();
  }, [loadCore]);

  const revoke = async (memory: MemoryView) => {
    try {
      await client.send('POST', `/operator/memories/${memory.id}/revoke`);
      await load();
    } catch (cause) {
      setError(String(cause));
    }
  };

  const inspectHistory = async (memory: MemoryView) => {
    try {
      const [versions, audit] = await Promise.all([
        client.get<{ history: MemoryView[] }>(`/operator/memories/${memory.id}/history`),
        client.get<{ operations: MemoryOperationView[] }>(
          `/operator/memories/${memory.id}/operations`,
        ),
      ]);
      setHistory(versions.history);
      setOperations(audit.operations);
    } catch (cause) {
      setError(String(cause));
    }
  };

  const editCore = (block: CoreBlockView) => {
    setCoreDraft({
      label: block.label,
      subject_identity_id: block.subject_identity_id ?? '',
      content: block.content,
      token_budget: block.token_budget,
    });
  };

  const saveCore = async () => {
    try {
      await client.send('PUT', `/operator/core-blocks/${coreDraft.label}`, {
        subject_identity_id:
          coreDraft.label === 'user_profile' ? coreDraft.subject_identity_id.trim() : null,
        content: coreDraft.content,
        token_budget: coreDraft.token_budget,
      });
      await loadCore();
      setError(null);
    } catch (cause) {
      setError(String(cause));
    }
  };

  return (
    <div className="mem-view view-enter">
      <section className="card" aria-label="核心记忆块">
        <div className="mem-toolbar">
          <div>
            <strong>核心记忆块</strong>
            <p className="view-footnote">每回合固定注入，保存时执行 token 上限与版本审计。</p>
          </div>
          <div className="mem-chips">
            {coreBlocks.map((block) => (
              <button
                key={block.id}
                type="button"
                className="pill-button"
                onClick={() => editCore(block)}
              >
                {block.label === 'persona' ? '人设' : block.subject_identity_id} · v{block.version}
              </button>
            ))}
          </div>
        </div>
        <div className="mem-toolbar">
          <label>
            类型
            <select
              aria-label="核心块类型"
              value={coreDraft.label}
              onChange={(event) =>
                setCoreDraft((draft) => ({
                  ...draft,
                  label: event.target.value as CoreDraft['label'],
                }))
              }
            >
              <option value="persona">persona</option>
              <option value="user_profile">user_profile</option>
            </select>
          </label>
          {coreDraft.label === 'user_profile' && (
            <label>
              主体
              <input
                aria-label="核心块主体"
                value={coreDraft.subject_identity_id}
                onChange={(event) =>
                  setCoreDraft((draft) => ({ ...draft, subject_identity_id: event.target.value }))
                }
              />
            </label>
          )}
          <label>
            Token 上限
            <input
              aria-label="核心块 Token 上限"
              type="number"
              min={50}
              max={20000}
              value={coreDraft.token_budget}
              onChange={(event) =>
                setCoreDraft((draft) => ({ ...draft, token_budget: Number(event.target.value) }))
              }
            />
          </label>
        </div>
        <textarea
          aria-label="核心块内容"
          value={coreDraft.content}
          onChange={(event) =>
            setCoreDraft((draft) => ({ ...draft, content: event.target.value }))
          }
        />
        <button type="button" className="pill-button" onClick={() => void saveCore()}>
          保存核心块
        </button>
      </section>

      <div className="mem-toolbar">
        <div className="segmented" role="group" aria-label="范围筛选">
          {SCOPE_FILTERS.map((filter) => (
            <button
              key={filter.id}
              type="button"
              data-active={scope === filter.id}
              aria-pressed={scope === filter.id}
              onClick={() => setScope(filter.id)}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="switch"
          role="switch"
          aria-checked={includeHistory}
          onClick={() => setIncludeHistory((current) => !current)}
        >
          <span className="switch-track" data-on={includeHistory} aria-hidden="true">
            <span className="switch-knob" />
          </span>
          <span>包含历史版本</span>
        </button>
      </div>
      {error && <p className="panel-error" role="alert">{error}</p>}
      {memories === null ? (
        <section className="card"><p className="panel-empty">正在加载记忆…</p></section>
      ) : memories.length === 0 ? (
        <section className="card"><p className="panel-empty">没有符合当前筛选的记忆。</p></section>
      ) : (
        <ul className="mem-list" aria-label="记忆列表">
          {memories.map((memory) => (
            <li
              key={memory.id}
              className="card mem-card"
              data-revoked={memoryState(memory) !== 'active'}
            >
              <div className="mem-main">
                <div className="mem-chips">
                  <span className="scope-chip" data-scope={memory.scope}>{scopeLabel(memory.scope)}</span>
                  <span className="meta-chip">{privacyLabel(memory.privacy)}</span>
                  <span className="meta-chip">{memoryKindLabel(memory.kind)}</span>
                  <span className="meta-chip">
                    {memoryState(memory) === 'active'
                      ? '有效'
                      : memoryState(memory) === 'invalidated'
                        ? '已失效'
                        : '已撤销'}
                  </span>
                </div>
                <p className="mem-content">{memory.content}</p>
                <small className="mem-foot">
                  {memory.subject_identity_id ?? memory.conversation_stable_key ?? 'global'} · 可信度{' '}
                  {memory.confidence.toFixed(2)}
                </small>
              </div>
              <div className="mem-chips">
                <button type="button" className="pill-button" onClick={() => void inspectHistory(memory)}>
                  版本
                </button>
                {memoryState(memory) === 'active' && (
                  <button
                    type="button"
                    className="pill-button pill-button--danger"
                    onClick={() => void revoke(memory)}
                  >
                    撤销
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {history && (
        <section className="card" aria-label="记忆版本历史">
          <strong>版本历史</strong>
          <ol>
            {history.map((item) => (
              <li key={item.id}>{item.state} · {item.content}</li>
            ))}
          </ol>
          <strong>操作审计</strong>
          <ol>
            {operations.map((item) => (
              <li key={item.id}>{item.operation} · {item.source}</li>
            ))}
          </ol>
        </section>
      )}
      <p className="view-footnote">
        语义更新与删除进入可追溯失效链；/forget 与运营者撤销继续走隐私撤销路径。
      </p>
    </div>
  );
}
