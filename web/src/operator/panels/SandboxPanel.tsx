import { FormEvent, useEffect, useMemo, useState } from 'react';

import type { OperatorClient, SandboxSessionView } from '../api';
import { TraceTimeline } from '../components/TraceTimeline';
import { outcomeMeta, triggerLabel } from '../labels';

function newSessionId() {
  return `sandbox-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function SandboxPanel({ client }: { client: OperatorClient }) {
  const initialSession = useMemo(newSessionId, []);
  const [sessionId, setSessionId] = useState(initialSession);
  const [text, setText] = useState('');
  const [imageUrl, setImageUrl] = useState('');
  const [session, setSession] = useState<SandboxSessionView>({
    status: 'pending',
    session_id: initialSession,
  });
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!waiting) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const next = await client.get<SandboxSessionView>(
          `/operator/sandbox/${encodeURIComponent(sessionId)}`,
        );
        if (!cancelled) {
          setSession(next);
          const outbound = next.messages?.some((message) => message.direction === 'outbound');
          if (outbound) setWaiting(false);
        }
      } catch (cause) {
        if (!cancelled) {
          setError(String(cause));
          setWaiting(false);
        }
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 900);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, sessionId, waiting]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const cleanText = text.trim();
    const cleanImage = imageUrl.trim();
    if (!cleanText && !cleanImage) return;
    setError(null);
    setWaiting(true);
    try {
      await client.send('POST', '/operator/sandbox/messages', {
        session_id: sessionId,
        text: cleanText || null,
        image_urls: cleanImage ? [cleanImage] : [],
      });
      setText('');
      setImageUrl('');
    } catch (cause) {
      setError(String(cause));
      setWaiting(false);
    }
  };

  const reset = () => {
    const next = newSessionId();
    setSessionId(next);
    setSession({ status: 'pending', session_id: next });
    setWaiting(false);
    setError(null);
  };

  return (
    <div className="sandbox-view view-enter">
      <section className="card sandbox-session-card">
        <div className="sandbox-session-heading">
          <div>
            <span className="tone-chip" data-tone={waiting ? 'orange' : session.status === 'ready' ? 'green' : 'gray'}>
              {waiting ? '处理中' : session.status === 'ready' ? '链路就绪' : '等待消息'}
            </span>
            <h2>WebChat 多模态沙盒</h2>
            <p>消息会经过真实 Streams、Agent、模型路由和出站链路，但不会写入长期记忆或触发主动消息。</p>
          </div>
          <button type="button" className="pill-button pill-button--neutral" onClick={reset}>
            新建会话
          </button>
        </div>
        <code className="sandbox-session-id">{sessionId}</code>
      </section>

      <div className="sandbox-grid">
        <section className="card sandbox-transcript-card" aria-label="沙盒消息记录">
          <div className="card-heading">
            <h2>消息记录</h2>
            <small>{session.messages?.length ?? 0} 条</small>
          </div>
          {(session.messages?.length ?? 0) === 0 ? (
            <p className="panel-empty">发送文本或图片 URL 后，真实链路的回复会显示在这里。</p>
          ) : (
            <div className="transcript">
              {session.messages?.map((message, index) => {
                const outbound = message.direction === 'outbound';
                return (
                  <div
                    key={`${message.trace_id ?? index}-${index}`}
                    className="transcript-row"
                    data-direction={outbound ? 'out' : 'in'}
                  >
                    <div className="transcript-group">
                      <small>{outbound ? 'bot' : 'operator'}</small>
                      <div className="bubble">{message.text || '[富媒体消息]'}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {waiting && <p className="sandbox-progress">正在经过消息队列、Agent 与出站链路…</p>}
          {error && <p className="panel-error" role="alert">{error}</p>}
        </section>

        <section className="card sandbox-turn-card" aria-label="沙盒轮次">
          <div className="card-heading">
            <h2>轮次结果</h2>
            <small>{session.turns?.length ?? 0} 轮</small>
          </div>
          {(session.turns?.length ?? 0) === 0 ? (
            <p className="panel-empty">完成一次回复后显示模型、触发方式、Token 与延迟。</p>
          ) : (
            <ul className="turn-list">
              {session.turns?.map((turn) => {
                const meta = outcomeMeta(turn.outcome);
                return (
                  <li key={turn.id}>
                    <span className="tone-chip" data-tone={meta.tone}>{meta.label}</span>
                    <span className="turn-trigger">{triggerLabel(turn.trigger)} · {turn.model ?? '无模型'}</span>
                    <span className="turn-meta">
                      {turn.prompt_tokens + turn.completion_tokens} tok · {turn.latency_ms} ms
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      <TraceTimeline traces={session.traces ?? []} title="沙盒链路追踪" />

      <form className="card sandbox-composer" onSubmit={(event) => void submit(event)}>
        <div className="card-heading card-heading--stacked">
          <h2>发送测试消息</h2>
          <small>可单独发送文本或图片，也可以同时发送以验证视觉模型链路。</small>
        </div>
        <div className="sandbox-fields">
          <label>
            消息
            <textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="输入要测试的消息"
            />
          </label>
          <label>
            图片 URL（可选）
            <input
              type="url"
              value={imageUrl}
              onChange={(event) => setImageUrl(event.target.value)}
              placeholder="https://…"
            />
          </label>
        </div>
        <button
          type="submit"
          className="pill-button pill-button--primary sandbox-submit"
          disabled={waiting || (!text.trim() && !imageUrl.trim())}
        >
          {waiting ? '正在发送…' : '发送到真实链路'}
        </button>
      </form>
    </div>
  );
}
