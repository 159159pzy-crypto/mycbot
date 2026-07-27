import { FormEvent, useEffect, useMemo, useState } from 'react';

import type { OperatorClient, SandboxSessionView } from '../api';

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
    <div className="op-sandbox">
      <div className="op-sandbox__bar">
        <div>
          <h3>WebChat 沙盒</h3>
          <p className="op-hint">会走真实 Streams 与 Agent 流程，但不会写入记忆或主动消息策略。</p>
        </div>
        <button type="button" onClick={reset}>新会话</button>
      </div>
      <code className="op-session-id">{sessionId}</code>
      <ul className="op-transcript" aria-label="Sandbox transcript">
        {(session.messages ?? []).map((message, index) => (
          <li key={`${message.trace_id ?? index}-${index}`} data-direction={message.direction}>
            <code>{message.direction === 'inbound' ? 'operator' : 'bot'}</code>
            <span>{message.text || '[富媒体消息]'}</span>
          </li>
        ))}
      </ul>
      {waiting && <p className="op-hint">正在经过消息队列、Agent 和出站链路…</p>}
      {error && <p className="op-error">{error}</p>}
      <form className="op-sandbox__composer" onSubmit={(event) => void submit(event)}>
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
        <button type="submit" disabled={waiting || (!text.trim() && !imageUrl.trim())}>
          发送到真实链路
        </button>
      </form>
    </div>
  );
}
