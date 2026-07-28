import { useCallback, useEffect, useState } from 'react';

import type {
  ApprovalRequestView,
  EvaluationCaseView,
  EvaluationRunDetailView,
  EvaluationRunView,
  ModerationAuditView,
  ModerationPolicyView,
  OperatorClient,
} from '../api';

export function SafetyPanel({ client }: { client: OperatorClient }) {
  const [policy, setPolicy] = useState<ModerationPolicyView | null>(null);
  const [audits, setAudits] = useState<ModerationAuditView[]>([]);
  const [approvals, setApprovals] = useState<ApprovalRequestView[]>([]);
  const [cases, setCases] = useState<EvaluationCaseView[]>([]);
  const [runs, setRuns] = useState<EvaluationRunView[]>([]);
  const [runDetail, setRunDetail] = useState<EvaluationRunDetailView | null>(null);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [judgeEnabled, setJudgeEnabled] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const detailPromise = selectedRun
        ? client.get<EvaluationRunDetailView>(`/operator/evaluations/runs/${selectedRun}`)
        : Promise.resolve(null);
      const [policyPayload, auditPayload, approvalPayload, casePayload, runPayload, detail] =
        await Promise.all([
          client.get<{ policy: ModerationPolicyView }>('/operator/safety/moderation-policy'),
          client.get<{ entries: ModerationAuditView[] }>('/operator/safety/moderation-audit'),
          client.get<{ requests: ApprovalRequestView[] }>('/operator/safety/approvals'),
          client.get<{ cases: EvaluationCaseView[] }>('/operator/evaluations/cases'),
          client.get<{ runs: EvaluationRunView[] }>('/operator/evaluations/runs'),
          detailPromise,
        ]);
      setPolicy(policyPayload.policy);
      setAudits(auditPayload.entries);
      setApprovals(approvalPayload.requests);
      setCases(casePayload.cases);
      setRuns(runPayload.runs);
      setRunDetail(detail);
      setError(null);
    } catch (cause) {
      setError(String(cause));
    }
  }, [client, selectedRun]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 2500);
    return () => window.clearInterval(timer);
  }, [load]);

  const savePolicy = async () => {
    if (!policy) return;
    const payload = await client.send<{ policy: ModerationPolicyView }>(
      'PUT',
      '/operator/safety/moderation-policy',
      policy,
    );
    setPolicy(payload.policy);
    setNotice('审核策略已保存，下一条消息生效。');
  };

  const decide = async (id: string, approved: boolean) => {
    await client.send('POST', `/operator/safety/approvals/${id}`, { approved });
    setNotice(approved ? '已批准这一次工具调用。' : '已拒绝这一次工具调用。');
    await load();
  };

  const startRun = async () => {
    const payload = await client.send<{ run_id: string; queued: number }>(
      'POST',
      '/operator/evaluations/runs',
      {
        name: `回归评测 ${new Date().toLocaleString()}`,
        case_ids: selected,
        judge_enabled: judgeEnabled,
      },
    );
    setSelectedRun(payload.run_id);
    setNotice(`评测 ${payload.run_id.slice(0, 8)} 已排队，共 ${payload.queued} 条。`);
    await load();
  };

  if (error) {
    return <section className="card"><p className="panel-error">{error}</p></section>;
  }
  if (!policy) {
    return <section className="card"><p className="panel-empty">正在加载安全策略…</p></section>;
  }

  return (
    <div className="panel-grid view-enter safety-view">
      {notice && <p className="panel-saved safety-notice" role="status">{notice}</p>}
      <section className="card">
        <div className="card-heading"><h2>双向审核</h2><small>local / API / plugin</small></div>
        <label className="field-row">
          <span>启用审核</span>
          <input type="checkbox" checked={policy.enabled} onChange={(event) => setPolicy({ ...policy, enabled: event.target.checked })} />
        </label>
        <fieldset className="safety-options">
          <legend>审核后端</legend>
          {(['local', 'api', 'plugin'] as const).map((backend) => (
            <label key={backend}>
              <input
                type="checkbox"
                checked={policy.backends.includes(backend)}
                onChange={(event) => setPolicy({
                  ...policy,
                  backends: event.target.checked
                    ? [...policy.backends, backend]
                    : policy.backends.filter((item) => item !== backend),
                })}
              />
              <span>{backend}</span>
            </label>
          ))}
        </fieldset>
        {(['inbound', 'outbound'] as const).map((point) => (
          <fieldset className="safety-point" key={point}>
            <legend>{point === 'inbound' ? '入站策略' : '出站策略'}</legend>
            <label>
              <input
                type="checkbox"
                checked={policy[point].enabled}
                onChange={(event) => setPolicy({
                  ...policy,
                  [point]: { ...policy[point], enabled: event.target.checked },
                })}
              />
              <span>启用</span>
            </label>
            <select
              aria-label={`${point} 命中动作`}
              value={policy[point].action}
              onChange={(event) => setPolicy({
                ...policy,
                [point]: {
                  ...policy[point],
                  action: event.target.value as 'direct_output' | 'overridden',
                },
              })}
            >
              <option value="direct_output">直接返回预设响应</option>
              <option value="overridden">替换内容后继续</option>
            </select>
            <input
              aria-label={`${point} 预设响应`}
              value={policy[point].preset_response}
              onChange={(event) => setPolicy({
                ...policy,
                [point]: { ...policy[point], preset_response: event.target.value },
              })}
            />
          </fieldset>
        ))}
        <label className="field-row">
          <span>后端失败</span>
          <select value={policy.fail_mode} onChange={(event) => setPolicy({ ...policy, fail_mode: event.target.value as 'open' | 'closed' })}>
            <option value="open">放行并审计</option>
            <option value="closed">阻断并审计</option>
          </select>
        </label>
        <label className="stack-field">
          <span>本地词表（每行一个）</span>
          <textarea rows={8} value={policy.keywords.join('\n')} onChange={(event) => setPolicy({ ...policy, keywords: event.target.value.split('\n').map((item) => item.trim()).filter(Boolean) })} />
        </label>
        <div className="inline-actions"><button type="button" className="primary-button" onClick={() => void savePolicy()}>保存策略</button></div>
      </section>
      <section className="card">
        <div className="card-heading"><h2>待审工具调用</h2><small>{approvals.length} 条</small></div>
        {approvals.length === 0 ? (
          <p className="panel-empty">当前没有待审批调用。</p>
        ) : (
          <ul className="approval-list">
            {approvals.map((item) => (
              <li key={item.id}>
                <span><code>{item.tool_id}</code><small>{item.conversation_stable_key}</small></span>
                <span className="inline-actions">
                  <button type="button" className="pill-button" onClick={() => void decide(item.id, true)}>批准一次</button>
                  <button type="button" className="pill-button pill-button--danger" onClick={() => void decide(item.id, false)}>拒绝</button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="card safety-wide">
        <div className="card-heading"><h2>回归评测</h2><small>影子模式，不向平台发消息</small></div>
        <div className="eval-cases">
          {cases.map((item) => (
            <label key={item.id}>
              <input type="checkbox" checked={selected.includes(item.id)} onChange={(event) => setSelected(event.target.checked ? [...selected, item.id] : selected.filter((id) => id !== item.id))} />
              <span><strong>{item.name}</strong><small>{item.question}</small></span>
            </label>
          ))}
        </div>
        <div className="inline-actions">
          <button type="button" className="primary-button" disabled={cases.length === 0} onClick={() => void startRun()}>运行所选（未选则全部）</button>
          <label className="judge-toggle"><input type="checkbox" checked={judgeEnabled} onChange={(event) => setJudgeEnabled(event.target.checked)} />启用 LLM 裁判</label>
        </div>
        <ul className="turn-list">
          {runs.map((run) => (
            <li key={run.id}>
              <span className="tone-chip" data-tone={run.failed > 0 ? 'danger' : run.status === 'COMPLETED' ? 'success' : 'warning'}>{run.status}</span>
              <button type="button" className="run-link" onClick={() => setSelectedRun(run.id)}>{run.name}</button>
              <span className="turn-meta">{run.passed}/{run.total} 通过 · {run.failed} 失败</span>
            </li>
          ))}
        </ul>
        {runDetail && (
          <div className="evaluation-detail">
            <div className="card-heading">
              <h3>逐条结果</h3>
              <small>{runDetail.run.model_channel || '自动渠道'} · persona {runDetail.run.persona_version_id?.slice(0, 8) || '默认'}</small>
            </div>
            {runDetail.results.map((result) => (
              <details key={result.id} open={!result.passed}>
                <summary>
                  <span className="tone-chip" data-tone={result.passed ? 'success' : result.status === 'COMPLETED' ? 'danger' : 'warning'}>
                    {result.status === 'COMPLETED' ? (result.passed ? '通过' : '失败') : result.status}
                  </span>
                  <strong>{result.case_snapshot.name}</strong>
                  <small>{result.model_channel || '—'} / {result.model || '—'}</small>
                </summary>
                <p><b>问题：</b>{result.case_snapshot.question}</p>
                <p><b>回答：</b>{result.response || '尚未生成'}</p>
                <ul className="assertion-list">
                  {result.assertions.map((assertion, index) => (
                    <li key={`${assertion.kind}-${index}`} data-passed={assertion.passed}>
                      <code>{assertion.kind}</code>
                      <span>期望 {JSON.stringify(assertion.expected)}</span>
                      <span>实际 {JSON.stringify(assertion.actual)}</span>
                    </li>
                  ))}
                </ul>
                {(result.tool_calls.length > 0 || result.citations.length > 0) && (
                  <p className="turn-meta">工具 {result.tool_calls.join(', ') || '无'} · 引用 {result.citations.length}</p>
                )}
                {result.judge_reason && <p className="turn-meta">裁判 {result.judge_score ?? '—'} · {result.judge_reason}</p>}
              </details>
            ))}
          </div>
        )}
      </section>
      <section className="card safety-wide">
        <div className="card-heading"><h2>审核审计</h2><small>最近 {audits.length} 条</small></div>
        <ul className="turn-list">
          {audits.slice(0, 30).map((entry) => (
            <li key={entry.id}>
              <span className="tone-chip" data-tone={entry.flagged ? 'danger' : 'success'}>{entry.flagged ? '拦截' : '放行'}</span>
              <span className="turn-trigger">{entry.point} · {entry.backend} · {entry.reason}<small>{entry.content_preview}</small></span>
              <span className="turn-meta">{entry.duration_ms} ms</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
