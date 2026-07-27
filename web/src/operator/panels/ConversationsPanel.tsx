import { useEffect, useRef, useState } from 'react';

import type {
  ConversationSummary,
  MessageView,
  OperatorClient,
  TraceSpanView,
  TurnView,
} from '../api';
import { TraceTimeline } from '../components/TraceTimeline';
import { chatKindLabel, outcomeMeta, platformLabel, triggerLabel } from '../labels';

type DetailState =
  | { kind: 'idle' }
  | { kind: 'loading'; conversation: ConversationSummary }
  | { kind: 'error'; conversation: ConversationSummary; message: string }
  | {
      kind: 'ready';
      conversation: ConversationSummary;
      messages: MessageView[];
      turns: TurnView[];
      traces: TraceSpanView[];
    };

function turnTools(turn: TurnView): string {
  if (turn.tool_invocations.length === 0) return '';
  const parts = turn.tool_invocations.map(
    (invocation) =>
      `${invocation.tool_id}${invocation.ok ? '' : `(${invocation.error_code ?? 'failed'})`}`,
  );
  return ` · 工具: ${parts.join(', ')}`;
}

export function ConversationsPanel({ client }: { client: OperatorClient }) {
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [detail, setDetail] = useState<DetailState>({ kind: 'idle' });
  const [error, setError] = useState<string | null>(null);
  const openSeq = useRef(0);

  const open = async (conversation: ConversationSummary) => {
    const seq = ++openSeq.current;
    setDetail({ kind: 'loading', conversation });
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
      if (openSeq.current !== seq) return;
      setDetail({
        kind: 'ready',
        conversation,
        messages: messages.messages,
        turns: turns.turns,
        traces: traces.traces ?? [],
      });
    } catch (cause) {
      if (openSeq.current !== seq) return;
      setDetail({ kind: 'error', conversation, message: String(cause) });
    }
  };

  useEffect(() => {
    let cancelled = false;
    client
      .get<{ conversations: ConversationSummary[] }>('/operator/conversations')
      .then((payload) => {
        if (cancelled) return;
        setConversations(payload.conversations);
        if (payload.conversations.length > 0) {
          void open(payload.conversations[0]);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(String(cause));
      });
    return () => {
      cancelled = true;
      openSeq.current += 1;
    };
  }, [client]);

  if (error) {
    return (
      <section className="card view-enter">
        <p className="panel-error" role="alert">
          {error}
        </p>
      </section>
    );
  }
  if (conversations === null) {
    return (
      <section className="card view-enter">
        <p className="panel-empty">正在加载会话…</p>
      </section>
    );
  }
  if (conversations.length === 0) {
    return (
      <section className="card view-enter">
        <p className="panel-empty">暂无会话记录。</p>
      </section>
    );
  }

  const selectedId = detail.kind === 'idle' ? null : detail.conversation.id;

  return (
    <div className="conv-view view-enter">
      <section className="card conv-list" aria-label="会话列表">
        {conversations.map((conversation) => (
          <button
            key={conversation.id}
            type="button"
            className="conv-item"
            data-active={conversation.id === selectedId}
            aria-pressed={conversation.id === selectedId}
            onClick={() => void open(conversation)}
          >
            <span className="conv-item-top">
              <span className="platform-chip" data-platform={conversation.platform}>
                {platformLabel(conversation.platform)}
              </span>
              <span className="conv-kind">{chatKindLabel(conversation.chat_kind)}</span>
              <span className="conv-count">{conversation.message_count} 条</span>
            </span>
            <code>{conversation.chat_id}</code>
          </button>
        ))}
      </section>
      <div className="conv-detail">
        {detail.kind === 'idle' && (
          <section className="card">
            <p className="panel-empty">从左侧选择一个会话。</p>
          </section>
        )}
        {detail.kind === 'loading' && (
          <section className="card">
            <p className="panel-empty">正在加载会话详情…</p>
          </section>
        )}
        {detail.kind === 'error' && (
          <section className="card">
            <p className="panel-error" role="alert">
              {detail.message}
            </p>
          </section>
        )}
        {detail.kind === 'ready' && (
          <>
            <section className="card" aria-label="消息记录">
              <div className="card-heading">
                <h2>消息记录</h2>
                <small>
                  {platformLabel(detail.conversation.platform)} ·{' '}
                  {chatKindLabel(detail.conversation.chat_kind)} ·{' '}
                  {detail.conversation.chat_id}
                </small>
              </div>
              {detail.messages.length === 0 ? (
                <p className="panel-empty">此会话暂无消息。</p>
              ) : (
                <div className="transcript">
                  {detail.messages.map((message, index) => {
                    const outbound = message.direction === 'outbound';
                    return (
                      <div
                        key={index}
                        className="transcript-row"
                        data-direction={outbound ? 'out' : 'in'}
                      >
                        <div className="transcript-group">
                          <small>{outbound ? 'bot' : message.sender_identity_id}</small>
                          <div className="bubble">{message.text || '[非文本]'}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
            <section className="card" aria-label="轮次明细">
              <div className="card-heading">
                <h2>轮次明细</h2>
              </div>
              {detail.turns.length === 0 ? (
                <p className="panel-empty">暂无轮次记录。</p>
              ) : (
                <ul className="turn-list">
                  {detail.turns.map((turn) => {
                    const meta = outcomeMeta(turn.outcome);
                    return (
                      <li key={turn.id}>
                        <span className="tone-chip" data-tone={meta.tone}>
                          {meta.label}
                        </span>
                        <span className="turn-trigger">
                          {triggerLabel(turn.trigger)}
                          {turnTools(turn)}
                        </span>
                        <span className="turn-meta">
                          {turn.prompt_tokens + turn.completion_tokens} tok ·{' '}
                          {turn.latency_ms} ms
                        </span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
            <TraceTimeline traces={detail.traces} />
          </>
        )}
      </div>
    </div>
  );
}
