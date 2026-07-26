import { useCallback, useEffect, useState } from 'react';

import type { MemoryView, OperatorClient } from '../api';

export function MemoriesPanel({ client }: { client: OperatorClient }) {
  const [memories, setMemories] = useState<MemoryView[] | null>(null);
  const [scope, setScope] = useState<string>('');
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (scope) params.set('scope', scope);
      if (includeRevoked) params.set('include_revoked', 'true');
      const query = params.toString();
      const payload = await client.get<{ memories: MemoryView[] }>(
        `/operator/memories${query ? `?${query}` : ''}`,
      );
      setMemories(payload.memories);
      setError(null);
    } catch (cause) {
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
    <div>
      <div className="op-filters">
        <label>
          Scope
          <select value={scope} onChange={(event) => setScope(event.target.value)}>
            <option value="">all</option>
            <option value="SUBJECT">SUBJECT</option>
            <option value="CONVERSATION">CONVERSATION</option>
            <option value="GLOBAL">GLOBAL</option>
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={includeRevoked}
            onChange={(event) => setIncludeRevoked(event.target.checked)}
          />
          Include revoked
        </label>
      </div>
      {error && <p className="op-error">{error}</p>}
      {memories === null ? (
        <p className="op-empty">Loading memories…</p>
      ) : memories.length === 0 ? (
        <p className="op-empty">No memories match the current filters.</p>
      ) : (
        <ul className="op-list op-list--memories" aria-label="Memories">
          {memories.map((memory) => (
            <li key={memory.id} data-revoked={memory.revoked_at !== null}>
              <div>
                <code>
                  {memory.scope} / {memory.privacy} / {memory.kind}
                </code>
                <span>{memory.content}</span>
                <small>
                  {memory.subject_identity_id ?? memory.conversation_stable_key ?? 'global'} ·
                  conf {memory.confidence.toFixed(2)}
                  {memory.revoked_at !== null && ` · revoked (${memory.revoked_reason})`}
                </small>
              </div>
              {memory.revoked_at === null && (
                <button type="button" onClick={() => void revoke(memory)}>
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
