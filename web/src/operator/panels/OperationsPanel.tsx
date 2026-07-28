import { useCallback, useEffect, useState } from 'react';

import type {
  ConversationSummary,
  DeadLetterEntry,
  OperatorClient,
  ProactiveConfig,
} from '../api';
import { platformLabel } from '../labels';

type StreamName = 'ingest' | 'outbound' | 'knowledge';

type OperationsState = {
  deadLetters: DeadLetterEntry[];
  conversations: ConversationSummary[];
  proactive: ProactiveConfig;
};

const STREAM_LABELS: Record<StreamName, string> = {
  ingest: '入站',
  outbound: '出站',
  knowledge: '知识摄取',
};

export function OperationsPanel({ client }: { client: OperatorClient }) {
  const [stream, setStream] = useState<StreamName>('ingest');
  const [state, setState] = useState<OperationsState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [target, setTarget] = useState('');
  const [message, setMessage] = useState('');
  const [enabled, setEnabled] = useState<string[]>([]);

  const load = useCallback(async () => {
    try {
      const [dead, conversations, proactive] = await Promise.all([
        client.get<{ entries: DeadLetterEntry[] }>(
          `/operator/operations/dead-letters?stream=${stream}&limit=100`,
        ),
        client.get<{ conversations: ConversationSummary[] }>(
          '/operator/conversations?limit=500',
        ),
        client.get<ProactiveConfig>('/operator/config/proactive'),
      ]);
      const available = conversations.conversations.filter((item) => !item.ephemeral);
      setState({ deadLetters: dead.entries, conversations: available, proactive });
      setEnabled(proactive.enabled_conversations);
      setTarget((current) => current || available[0]?.id || '');
      setError(null);
    } catch (nextError) {
      setError(String(nextError));
    }
  }, [client, stream]);

  useEffect(() => {
    void load();
  }, [load]);

  if (state === null) {
    return (
      <section className="card view-enter">
        <p className={error ? 'panel-error' : 'panel-empty'} role={error ? 'alert' : undefined}>
          {error ?? '正在加载运维数据…'}
        </p>
      </section>
    );
  }

  return (
    <div className="operations-view view-enter">
      {notice && <p className="operation-notice" role="status">{notice}</p>}
      {error && <p className="panel-error" role="alert">{error}</p>}

      <section className="card operations-deadletters">
        <div className="card-heading operations-heading">
          <div>
            <h2>死信检查与重放</h2>
            <small>确认根因修复后，才重放单条消息。</small>
          </div>
          <label>
            <span>流</span>
            <select value={stream} onChange={(event) => setStream(event.target.value as StreamName)}>
              {Object.entries(STREAM_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
        </div>
        {state.deadLetters.length === 0 ? (
          <p className="panel-empty">当前没有死信。</p>
        ) : (
          <ul className="dead-letter-list">
            {state.deadLetters.map((entry) => (
              <li key={entry.id}>
                <div>
                  <code>{entry.id}</code>
                  <p>{entry.preview}</p>
                </div>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={async () => {
                    try {
                      await client.send(
                        'POST',
                        `/operator/operations/dead-letters/${stream}/${entry.id}/replay`,
                      );
                      setNotice(`已重放 ${entry.id}`);
                      await load();
                    } catch (nextError) {
                      setError(String(nextError));
                    }
                  }}
                >
                  重放
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="operations-grid">
        <section className="card">
          <div className="card-heading"><h2>以 Bot 身份发消息</h2></div>
          <form
            className="operations-form"
            onSubmit={async (event) => {
              event.preventDefault();
              if (!target || !message.trim()) return;
              try {
                await client.send('POST', '/operator/operations/messages', {
                  conversation_id: target,
                  text: message.trim(),
                });
                setMessage('');
                setNotice('消息已进入出站队列。');
              } catch (nextError) {
                setError(String(nextError));
              }
            }}
          >
            <label>
              <span>会话</span>
              <select value={target} onChange={(event) => setTarget(event.target.value)}>
                {state.conversations.map((conversation) => (
                  <option key={conversation.id} value={conversation.id}>
                    {platformLabel(conversation.platform)} · {conversation.chat_id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>消息</span>
              <textarea
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                placeholder="输入要主动发送的内容"
                rows={5}
              />
            </label>
            <button type="submit" className="primary-button">发送</button>
          </form>
        </section>

        <section className="card">
          <div className="card-heading">
            <h2>主动消息会话</h2>
            <small>{state.proactive.globally_enabled ? '全局功能已开启' : '全局功能未开启'}</small>
          </div>
          <form
            className="operations-form"
            onSubmit={async (event) => {
              event.preventDefault();
              try {
                await client.send('PUT', '/operator/config/proactive', {
                  enabled_conversations: enabled,
                });
                setNotice('主动消息会话已更新。');
              } catch (nextError) {
                setError(String(nextError));
              }
            }}
          >
            <label>
              <span>允许接收的会话</span>
              <select
                multiple
                size={Math.min(Math.max(state.conversations.length, 4), 10)}
                value={enabled}
                onChange={(event) => {
                  setEnabled(Array.from(event.target.selectedOptions, (option) => option.value));
                }}
              >
                {state.conversations.map((conversation) => (
                  <option key={conversation.stable_key} value={conversation.stable_key}>
                    {platformLabel(conversation.platform)} · {conversation.chat_id}
                  </option>
                ))}
              </select>
            </label>
            <p className="card-note">使用 Ctrl / Command 可多选，不再需要手填 stable_key。</p>
            <button type="submit" className="primary-button">保存选择</button>
          </form>
        </section>
      </div>
    </div>
  );
}
