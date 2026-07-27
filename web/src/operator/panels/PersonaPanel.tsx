import { useEffect, useState } from 'react';

import type { OperatorClient, PersonaConfig } from '../api';

export function PersonaPanel({ client }: { client: OperatorClient }) {
  const [text, setText] = useState<string | null>(null);
  const [defaultPrompt, setDefaultPrompt] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    client
      .get<PersonaConfig>('/operator/config/persona')
      .then((persona) => {
        if (!cancelled) {
          setText(persona.override ?? persona.default);
          setDefaultPrompt(persona.default);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  const save = async () => {
    if (text === null) return;
    try {
      await client.send('PUT', '/operator/config/persona', { system_prompt: text });
      setSaved(true);
      setError(null);
    } catch (cause) {
      setError(String(cause));
    }
  };

  if (error && text === null) {
    return (
      <section className="card view-enter">
        <p className="panel-error" role="alert">
          {error}
        </p>
      </section>
    );
  }
  if (text === null) {
    return (
      <section className="card view-enter">
        <p className="panel-empty">正在加载人设…</p>
      </section>
    );
  }

  return (
    <div className="persona-view view-enter">
      <section className="card persona-card">
        <div className="card-heading card-heading--stacked">
          <h2>系统提示词</h2>
          <small>
            智能体的系统提示词。保存后写入运行时覆盖，工作进程在下一轮对话自动生效，无需重启。
          </small>
        </div>
        <textarea
          rows={9}
          value={text}
          aria-label="系统提示词"
          onChange={(event) => {
            setText(event.target.value);
            setSaved(false);
          }}
        />
        <div className="persona-actions">
          <button
            type="button"
            className="pill-button pill-button--primary"
            onClick={() => void save()}
          >
            保存人设
          </button>
          <button
            type="button"
            className="pill-button pill-button--neutral"
            onClick={() => {
              setText(defaultPrompt);
              setSaved(false);
            }}
          >
            恢复默认
          </button>
          <span className="panel-saved" role="status">
            {saved ? '已保存，下一轮对话生效。' : ''}
          </span>
        </div>
        {error && (
          <p className="panel-error" role="alert">
            {error}
          </p>
        )}
      </section>
    </div>
  );
}
