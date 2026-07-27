import { useEffect, useMemo, useState } from 'react';

import type { ModelChannel, ModelsView, OperatorClient } from '../api';

function dollars(micros: number): string {
  return `$${(micros / 1_000_000).toFixed(6)}`;
}

function statusTone(status: string | undefined): 'green' | 'red' | 'gray' | 'orange' {
  if (status === 'success' || status === 'ok') return 'green';
  if (status === 'error' || status === 'failed') return 'red';
  if (status === undefined || status === 'untested') return 'gray';
  return 'orange';
}

export function ModelsPanel({ client }: { client: OperatorClient }) {
  const [view, setView] = useState<ModelsView | null>(null);
  const [draft, setDraft] = useState('[]');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    client
      .get<ModelsView>('/operator/models')
      .then((payload) => {
        if (!cancelled) {
          setView(payload);
          setDraft(JSON.stringify(payload.channels, null, 2));
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  const totals = useMemo(
    () =>
      (view?.usage ?? []).reduce(
        (sum, usage) => ({
          calls: sum.calls + usage.calls,
          tokens: sum.tokens + usage.prompt_tokens + usage.completion_tokens,
          cost: sum.cost + usage.cost_usd_micros,
        }),
        { calls: 0, tokens: 0, cost: 0 },
      ),
    [view],
  );

  const save = async () => {
    try {
      const channels = JSON.parse(draft) as ModelChannel[];
      if (!Array.isArray(channels)) throw new Error('渠道配置必须是 JSON 数组。');
      await client.send('PUT', '/operator/models', { channels });
      setView((current) => (current === null ? current : { ...current, channels }));
      setNotice('模型渠道已保存，工作进程会在缓存过期后刷新。');
      setError(null);
    } catch (cause) {
      setError(String(cause));
    }
  };

  const test = async (name: string) => {
    try {
      const result = await client.send<{
        ok: boolean;
        channel: string;
        model: string;
        latency_ms: number;
      }>('POST', `/operator/models/${encodeURIComponent(name)}/test`);
      setNotice(`${result.channel} / ${result.model} / ${result.latency_ms} ms`);
      setError(null);
    } catch (cause) {
      setError(String(cause));
    }
  };

  if (view === null && error === null) {
    return (
      <section className="card view-enter">
        <p className="panel-empty">正在加载模型渠道…</p>
      </section>
    );
  }

  if (view === null) {
    return (
      <section className="card view-enter">
        <p className="panel-error" role="alert">
          {error}
        </p>
      </section>
    );
  }

  return (
    <div className="models-view view-enter">
      <section className="card model-summary">
        <div className="card-heading card-heading--stacked">
          <h2>模型路由概览</h2>
          <small>
            配置来源：{view.source === 'runtime' ? '运行时配置' : '兼容配置'}。密钥只保存环境变量名称，不会在控制台展示真实值。
          </small>
        </div>
        <div className="model-stat-row">
          <div>
            <small>启用渠道</small>
            <strong>{view.channels.filter((channel) => channel.enabled).length}</strong>
          </div>
          <div>
            <small>调用次数</small>
            <strong>{totals.calls.toLocaleString()}</strong>
          </div>
          <div>
            <small>累计 Token</small>
            <strong>{totals.tokens.toLocaleString()}</strong>
          </div>
          <div>
            <small>估算成本</small>
            <strong>{dollars(totals.cost)}</strong>
          </div>
        </div>
      </section>

      <section className="model-channel-grid" aria-label="模型渠道状态">
        {view.channels.map((channel) => {
          const usage = view.usage.find((entry) => entry.channel === channel.name);
          const status = usage?.last_status ?? 'untested';
          return (
            <article className="card model-channel" key={channel.name}>
              <div className="model-channel-heading">
                <div>
                  <span className="tone-chip" data-tone={channel.enabled ? 'green' : 'gray'}>
                    {channel.enabled ? '已启用' : '已停用'}
                  </span>
                  <h2>{channel.name}</h2>
                </div>
                <span className="tone-chip" data-tone={statusTone(status)}>
                  {status === 'untested' ? '未测试' : status}
                </span>
              </div>
              <code className="model-endpoint">{channel.base_url}</code>
              <div className="model-map">
                {Object.entries(channel.model_map).map(([purpose, target]) => (
                  <span className="meta-chip" key={purpose}>
                    {purpose} <code>{target.model}</code>
                  </span>
                ))}
              </div>
              <div className="model-channel-foot">
                <span>{usage?.calls ?? 0} 次调用 · {dollars(usage?.cost_usd_micros ?? 0)}</span>
                <button
                  type="button"
                  className="pill-button pill-button--neutral"
                  onClick={() => void test(channel.name)}
                >
                  测试 {channel.name}
                </button>
              </div>
            </article>
          );
        })}
      </section>

      <div className="model-usage-grid">
        <section className="card">
          <div className="card-heading">
            <h2>每日用量</h2>
            <small>最近 30 天</small>
          </div>
          <ul className="model-usage-list" aria-label="每日模型用量">
            {view.daily_usage.map((entry) => (
              <li key={entry.day}>
                <code>{entry.day}</code>
                <span>{entry.calls} 次</span>
                <span>{(entry.prompt_tokens + entry.completion_tokens).toLocaleString()} tok</span>
                <strong>{dollars(entry.cost_usd_micros)}</strong>
              </li>
            ))}
          </ul>
        </section>
        <section className="card">
          <div className="card-heading">
            <h2>会话用量</h2>
            <small>最近 30 天</small>
          </div>
          <ul className="model-usage-list" aria-label="会话模型用量">
            {view.conversation_usage.map((entry, index) => (
              <li key={entry.conversation_id ?? `unattributed-${index}`}>
                <code>{entry.stable_key ?? '未归属'}</code>
                <span>{entry.calls} 次</span>
                <span>{(entry.prompt_tokens + entry.completion_tokens).toLocaleString()} tok</span>
                <strong>{dollars(entry.cost_usd_micros)}</strong>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <section className="card model-config">
        <div className="card-heading card-heading--stacked">
          <h2>渠道配置</h2>
          <small>保存后由工作进程按缓存周期重新载入。请仅填写环境变量名，不要粘贴密钥。</small>
        </div>
        <label htmlFor="model-channels-json">模型渠道 JSON</label>
        <textarea
          id="model-channels-json"
          rows={18}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          spellCheck={false}
        />
        <div className="model-config-actions">
          <button type="button" className="pill-button pill-button--primary" onClick={() => void save()}>
            保存渠道
          </button>
          {notice && <p className="panel-saved">{notice}</p>}
          {error && <p className="panel-error" role="alert">{error}</p>}
        </div>
      </section>
    </div>
  );
}
