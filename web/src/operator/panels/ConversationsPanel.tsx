import { useEffect, useState } from 'react';

import type {
  ConversationSummary,
  MessageView,
  OperatorClient,
  TraceSpanView,
  TurnView,
} from '../api';

type DetailState =
  | { kind: 'idle' }
  | { kind: 'loading'; id: string }
  | {
      kind: 'ready';
      id: string;
      messages: MessageView[];
      turns: TurnView[];
      traces: TraceSpanView[];
    };

export function ConversationsPanel({ client }: { client: OperatorClient }) {
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [detail, setDetail] = useState<DetailState>({ kind: 'idle' });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    client
      .get<{ conversations: ConversationSummary[] }>('/operator/conversations')
      .then((payload) => {
        if (!cancelled) setConversations(payload.conversations);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  const open = async (conversation: ConversationSummary) => {
    setDetail({ kind: 'loading', id: conversation.id });
    try {
      const [messages, turns, traces] = await Promise.all([
        client.get<{ messages: MessageView[] }>(
          `/operator/conversations/${conversation.id}/messages`,
        ),
        client.get<{ turns: TurnView[] }>(
          `/operator/conversations/${conversation.id}/turns`,
        ),
        client.get<{ traces: TraceSpanView[] }>(
          `/operator/conversations/${conversation.id}/traces`,
        ),
      ]);
      setDetail({
        kind: 'ready',
        id: conversation.id,
        messages: messages.messages,
        turns: turns.turns,
        traces: traces.traces,
      });
    } catch (cause) {
      setError(String(cause));
      setDetail({ kind: 'idle' });
    }
  };

  if (error) return <p className="op-error">{error}</p>;
  if (conversations === null) return <p className="op-empty">Loading conversations…</p>;
  if (conversations.length === 0) {
    return <p className="op-empty">No conversations recorded yet.</p>;
  }

  return (
    <div className="op-split">
      <ul className="op-list op-list--select" aria-label="Conversations">
        {conversations.map((conversation) => (
          <li key={conversation.id}>
            <button type="button" onClick={() => void open(conversation)}>
              <code>
                {conversation.platform} / {conversation.chat_kind} / {conversation.chat_id}
              </code>
              <span>{conversation.message_count} msgs</span>
              {conversation.ephemeral && <small>沙盒</small>}
            </button>
          </li>
        ))}
      </ul>
      <div className="op-detail">
        {detail.kind === 'idle' && <p className="op-empty">Select a conversation.</p>}
        {detail.kind === 'loading' && <p className="op-empty">Loading detail…</p>}
        {detail.kind === 'ready' && (
          <>
            <h3>Messages</h3>
            <ul className="op-transcript" aria-label="Messages">
              {detail.messages.map((message, index) => (
                <li key={index} data-direction={message.direction}>
                  <code>{message.direction === 'inbound' ? message.sender_identity_id : 'bot'}</code>
                  <span>{message.text || '[non-text]'}</span>
                </li>
              ))}
            </ul>
            <h3>Turns</h3>
            <ul className="op-list" aria-label="Turns">
              {detail.turns.map((turn) => (
                <li key={turn.id}>
                  <code>
                    {turn.outcome} / {turn.trigger}
                  </code>
                  <span>
                    {turn.prompt_tokens + turn.completion_tokens} tok · {turn.latency_ms}ms
                    {turn.tool_invocations.length > 0 &&
                      ` · tools: ${turn.tool_invocations
                        .map(
                          (invocation) =>
                            `${invocation.tool_id}${invocation.ok ? '' : `(${invocation.error_code ?? 'failed'})`}`,
                        )
                        .join(', ')}`}
                  </span>
                </li>
              ))}
            </ul>
            <details className="op-trace">
              <summary>追踪 · {detail.traces.length} 个阶段</summary>
              {detail.traces.length === 0 ? (
                <p className="op-empty">暂无追踪数据。</p>
              ) : (
                <ol className="op-list" aria-label="Trace spans">
                  {detail.traces.map((span) => (
                    <li key={span.id} data-status={span.status}>
                      <code>
                        {span.stage} / {span.status}
                      </code>
                      <span>{span.duration_ms}ms</span>
                      {Object.keys(span.attributes).length > 0 && (
                        <pre>{JSON.stringify(span.attributes, null, 2)}</pre>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </details>
          </>
        )}
      </div>
    </div>
  );
}
