import { useEffect, useState } from 'react';

import type { ModelChannel, ModelsView, OperatorClient } from '../api';

function dollars(micros: number): string {
  return `$${(micros / 1_000_000).toFixed(6)}`;
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

  const save = async () => {
    try {
      const channels = JSON.parse(draft) as ModelChannel[];
      if (!Array.isArray(channels)) throw new Error('Channels JSON must be an array.');
      await client.send('PUT', '/operator/models', { channels });
      setNotice('Model channels saved. Workers refresh them after the cache TTL.');
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
      setNotice(`${result.channel} / ${result.model} / ${result.latency_ms}ms`);
      setError(null);
    } catch (cause) {
      setError(String(cause));
    }
  };

  if (view === null && error === null) return <p className="op-empty">Loading models…</p>;

  return (
    <div>
      <h3>Model channels</h3>
      <p className="op-hint">
        Source: {view?.source ?? 'unknown'}. API keys are environment references only; secret
        values are never stored here.
      </p>
      <ul className="op-list" aria-label="Model channel health">
        {(view?.channels ?? []).map((channel) => {
          const usage = view?.usage.find((entry) => entry.channel === channel.name);
          return (
            <li key={channel.name}>
              <code>{channel.name}</code>
              <span>
                {usage?.last_status ?? 'untested'} · {usage?.calls ?? 0} calls ·{' '}
                {dollars(usage?.cost_usd_micros ?? 0)}
              </span>
              <button type="button" onClick={() => void test(channel.name)}>
                Test {channel.name}
              </button>
            </li>
          );
        })}
      </ul>
      <h3>Daily usage / 30d</h3>
      <ul className="op-list" aria-label="Daily model usage">
        {(view?.daily_usage ?? []).map((entry) => (
          <li key={entry.day}>
            <code>{entry.day}</code>
            <span>
              {entry.calls} calls ·{' '}
              {(entry.prompt_tokens + entry.completion_tokens).toLocaleString()} tokens ·{' '}
              {dollars(entry.cost_usd_micros)}
            </span>
          </li>
        ))}
      </ul>
      <h3>Conversation usage / 30d</h3>
      <ul className="op-list" aria-label="Conversation model usage">
        {(view?.conversation_usage ?? []).map((entry, index) => (
          <li key={entry.conversation_id ?? `unattributed-${index}`}>
            <code>{entry.stable_key ?? 'unattributed'}</code>
            <span>
              {entry.calls} calls ·{' '}
              {(entry.prompt_tokens + entry.completion_tokens).toLocaleString()} tokens ·{' '}
              {dollars(entry.cost_usd_micros)}
            </span>
          </li>
        ))}
      </ul>
      <label>
        Model channels JSON
        <textarea
          rows={18}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          spellCheck={false}
        />
      </label>
      <button type="button" onClick={() => void save()}>
        Save channels
      </button>
      {notice && <p className="op-hint">{notice}</p>}
      {error && <p className="op-error">{error}</p>}
    </div>
  );
}
