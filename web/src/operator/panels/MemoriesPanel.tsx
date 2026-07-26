import { useCallback, useEffect, useRef, useState } from 'react';

import type { MemoryView, OperatorClient } from '../api';
import { memoryKindLabel, privacyLabel, scopeLabel } from '../labels';

const SCOPE_FILTERS = [
  { id: '', label: '全部' },
  { id: 'SUBJECT', label: '主体' },
  { id: 'CONVERSATION', label: '会话' },
  { id: 'GLOBAL', label: '全局' },
] as const;

export function MemoriesPanel({ client }: { client: OperatorClient }) {
  const [memories, setMemories] = useState<MemoryView[] | null>(null);
  const [scope, setScope] = useState<string>('');
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const params = new URLSearchParams();
      if (scope) params.set('scope', scope);
      if (includeRevoked) params.set('include_revoked', 'true');
      const query = params.toString();
      const payload = await client.get<{ memories: MemoryView[] }>(
        `/operator/memories${query ? `?${query}` : ''}`,
      );
      if (loadSeq.current !== seq) return;
      setMemories(payload.memories);
      setError(null);
    } catch (cause) {
      if (loadSeq.current !== seq) return;
      setError(String(cause));
    }
  }, [client, scope, includeRevoked]);

  useEffect(() => {
    void load();
  }, [load]);

  const revoke = async (memory: MemoryView) => {
    try {
      await client.send('POST', `/operator/memories/${memory.id}/revoke`);
      await load();
    } catch (cause) {
      setError(String(cause));
    }
  };

  return (
    <div className="mem-view view-enter">
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
          aria-checked={includeRevoked}
          onClick={() => setIncludeRevoked((current) => !current)}
        >
          <span className="switch-track" data-on={includeRevoked} aria-hidden="true">
            <span className="switch-knob" />
          </span>
          <span>包含已撤销</span>
        </button>
      </div>
      {error && (
        <p className="panel-error" role="alert">
          {error}
        </p>
      )}
      {memories === null ? (
        <section className="card">
          <p className="panel-empty">正在加载记忆…</p>
        </section>
      ) : memories.length === 0 ? (
        <section className="card">
          <p className="panel-empty">没有符合当前筛选的记忆。</p>
        </section>
      ) : (
        <ul className="mem-list" aria-label="记忆列表">
          {memories.map((memory) => (
            <li
              key={memory.id}
              className="card mem-card"
              data-revoked={memory.revoked_at !== null}
            >
              <div className="mem-main">
                <div className="mem-chips">
                  <span className="scope-chip" data-scope={memory.scope}>
                    {scopeLabel(memory.scope)}
                  </span>
                  <span className="meta-chip">{privacyLabel(memory.privacy)}</span>
                  <span className="meta-chip">{memoryKindLabel(memory.kind)}</span>
                  {memory.revoked_at !== null && (
                    <span className="revoked-chip">
                      已撤销
                      {memory.revoked_reason ? ` · ${memory.revoked_reason}` : ''}
                    </span>
                  )}
                </div>
                <p className="mem-content">{memory.content}</p>
                <small className="mem-foot">
                  {memory.subject_identity_id ??
                    memory.conversation_stable_key ??
                    'global'}{' '}
                  · 可信度 {memory.confidence.toFixed(2)}
                </small>
              </div>
              {memory.revoked_at === null && (
                <button
                  type="button"
                  className="pill-button pill-button--danger"
                  onClick={() => void revoke(memory)}
                >
                  撤销
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <p className="view-footnote">
        记忆按范围与隐私级别存储，撤销后立即停止参与召回。
      </p>
    </div>
  );
}
