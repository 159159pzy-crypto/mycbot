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

  if (error && text === null) return <p className="op-error">{error}</p>;
  if (text === null) return <p className="op-empty">Loading persona…</p>;

  return (
    <div className="op-persona">
      <p className="op-hint">
        The agent's system prompt. Saving writes the runtime override; workers pick it
        up on their next turn without a restart.
      </p>
      <label>
        System prompt
        <textarea
          value={text}
          rows={8}
          onChange={(event) => {
            setText(event.target.value);
            setSaved(false);
          }}
        />
      </label>
      <div className="op-inline-form">
        <button type="button" onClick={() => void save()}>
          Save persona
        </button>
        <button
          type="button"
          onClick={() => {
            setText(defaultPrompt);
            setSaved(false);
          }}
        >
          Reset to default
        </button>
      </div>
      {saved && <p className="op-hint">Persona saved.</p>}
      {error && <p className="op-error">{error}</p>}
    </div>
  );
}
