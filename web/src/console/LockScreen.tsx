import { useState } from 'react';

import { LockIcon } from './icons';

export function LockScreen({
  notice,
  onUnlock,
}: {
  notice: string | null;
  onUnlock: (token: string) => void;
}) {
  const [draft, setDraft] = useState('');

  return (
    <div className="lock-screen">
      <form
        className="lock-card"
        onSubmit={(event) => {
          event.preventDefault();
          const token = draft.trim();
          if (token) {
            onUnlock(token);
            setDraft('');
          }
        }}
      >
        <span className="lock-glyph" aria-hidden="true">
          <LockIcon size={24} />
        </span>
        <h1 className="lock-title">控制台已锁定</h1>
        <p className="lock-hint">
          请输入操作员令牌（MYBOT_OPERATOR_TOKEN）。令牌仅保存在内存中，不会存储在浏览器里。
        </p>
        <input
          type="password"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          autoComplete="off"
          placeholder="操作员令牌"
          aria-label="操作员令牌"
          autoFocus
        />
        <button type="submit" className="pill-button pill-button--primary lock-submit">
          解锁控制台
        </button>
        {notice && (
          <p className="lock-error" role="alert">
            {notice}
          </p>
        )}
      </form>
    </div>
  );
}
