import { FormEvent, useCallback, useEffect, useState } from 'react';

import type {
  AnnotationView,
  KnowledgeDocumentView,
  KnowledgeSearchResult,
  OperatorClient,
} from '../api';

type Scope = 'ALL' | 'GLOBAL' | 'CONVERSATION';

export function KnowledgePanel({ client }: { client: OperatorClient }) {
  const [documents, setDocuments] = useState<KnowledgeDocumentView[]>([]);
  const [annotations, setAnnotations] = useState<AnnotationView[]>([]);
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<Scope>('ALL');
  const [stableKey, setStableKey] = useState('');
  const [topK, setTopK] = useState(5);
  const [threshold, setThreshold] = useState(0.35);
  const [results, setResults] = useState<KnowledgeSearchResult[]>([]);
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [annotationThreshold, setAnnotationThreshold] = useState(0.92);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [documentPayload, annotationPayload] = await Promise.all([
      client.get<{ documents: KnowledgeDocumentView[] }>('/operator/knowledge/documents'),
      client.get<{ annotations: AnnotationView[] }>('/operator/knowledge/annotations'),
    ]);
    setDocuments(documentPayload.documents);
    setAnnotations(annotationPayload.annotations);
  }, [client]);

  useEffect(() => {
    void refresh().catch((cause: unknown) => setError(String(cause)));
  }, [refresh]);

  const upload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const file = data.get('file');
    if (!(file instanceof File) || file.size === 0) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await client.upload<{ created: boolean }>('/operator/knowledge/documents', data);
      setNotice(payload.created ? '文档已入队，Knowledge Worker 正在处理。' : '相同作用域已有同一文档。');
      form.reset();
      await refresh();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await client.send<{ results: KnowledgeSearchResult[] }>(
        'POST',
        '/operator/knowledge/search',
        {
          query,
          top_k: topK,
          threshold,
          scope,
          conversation_stable_key: stableKey.trim() || null,
        },
      );
      setResults(payload.results);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };

  const createAnnotation = async (event: FormEvent) => {
    event.preventDefault();
    if (!question.trim() || !answer.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await client.send('POST', '/operator/knowledge/annotations', {
        scope: 'GLOBAL',
        question,
        answer,
        threshold: annotationThreshold,
        enabled: true,
      });
      setQuestion('');
      setAnswer('');
      setNotice('标注答案已保存并建立向量索引。');
      await refresh();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="knowledge-view view-enter">
      {error && <p className="panel-error" role="alert">{error}</p>}
      {notice && <p className="panel-saved" role="status">{notice}</p>}

      <section className="card knowledge-upload-card">
        <div className="card-heading">
          <div>
            <h2>文档摄取</h2>
            <small>支持 Markdown、TXT、PDF；上传只入队，解析和向量化由独立 Worker 完成。</small>
          </div>
        </div>
        <form className="knowledge-upload" onSubmit={(event) => void upload(event)}>
          <label>选择文档<input name="file" type="file" accept=".md,.markdown,.txt,.pdf" required /></label>
          <label>作用域
            <select name="scope" defaultValue="GLOBAL">
              <option value="GLOBAL">全局</option>
              <option value="CONVERSATION">指定会话</option>
            </select>
          </label>
          <label>会话 UUID<input name="conversation_id" placeholder="会话作用域时填写" /></label>
          <button className="pill-button primary" disabled={busy} type="submit">上传并摄取</button>
        </form>
        <div className="knowledge-documents">
          {documents.length === 0 ? <p className="panel-empty">暂无知识文档。</p> : documents.map((document) => (
            <article className="knowledge-document" key={document.id} data-status={document.status}>
              <div>
                <strong>{document.title}</strong>
                <small>{document.original_filename} · {document.scope} · {document.child_count} 个子块</small>
                {document.error_code && <code>{document.error_code}</code>}
              </div>
              <span className="tone-chip">{document.status}</span>
              <button className="pill-button danger" type="button" onClick={() => void client.send('DELETE', `/operator/knowledge/documents/${document.id}`).then(refresh)}>删除</button>
            </article>
          ))}
        </div>
      </section>

      <section className="card knowledge-search-card">
        <div className="card-heading"><h2>检索测试</h2><small>观察子块命中、父块上下文和语义分数。</small></div>
        <form className="knowledge-search-form" onSubmit={(event) => void search(event)}>
          <label className="span-2">Query<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入要验证的问题" /></label>
          <label>作用域<select value={scope} onChange={(event) => setScope(event.target.value as Scope)}><option value="ALL">全局 + 会话</option><option value="GLOBAL">仅全局</option><option value="CONVERSATION">仅会话</option></select></label>
          <label>会话 stable key<input value={stableKey} onChange={(event) => setStableKey(event.target.value)} placeholder="会话检索时填写" /></label>
          <label>Top K<input type="number" min="1" max="20" value={topK} onChange={(event) => setTopK(Number(event.target.value))} /></label>
          <label>阈值 <strong>{threshold.toFixed(2)}</strong><input type="range" min="0" max="1" step="0.01" value={threshold} onChange={(event) => setThreshold(Number(event.target.value))} /></label>
          <button className="pill-button primary" disabled={busy} type="submit">立即重检</button>
        </form>
        <div className="knowledge-results">
          {results.map((result) => (
            <article key={result.child_id}>
              <header><strong>{result.document_title}</strong><span>{result.score.toFixed(4)}</span></header>
              <div><small>命中子块</small><p>{result.child_content}</p></div>
              <div><small>父块上下文</small><p>{result.parent_content}</p></div>
            </article>
          ))}
          {results.length === 0 && <p className="panel-empty">运行检索后在这里查看召回结果。</p>}
        </div>
      </section>

      <section className="card annotation-card">
        <div className="card-heading"><h2>标注回复</h2><small>高阈值且具有明显分差时跳过聊天模型，仍经过出站审核。</small></div>
        <form className="annotation-form" onSubmit={(event) => void createAnnotation(event)}>
          <label>问题<input value={question} onChange={(event) => setQuestion(event.target.value)} /></label>
          <label>审核答案<textarea value={answer} onChange={(event) => setAnswer(event.target.value)} /></label>
          <label>命中阈值 <strong>{annotationThreshold.toFixed(2)}</strong><input type="range" min="0.7" max="1" step="0.01" value={annotationThreshold} onChange={(event) => setAnnotationThreshold(Number(event.target.value))} /></label>
          <button className="pill-button primary" disabled={busy} type="submit">保存标注</button>
        </form>
        <div className="annotation-list">
          {annotations.map((annotation) => (
            <article key={annotation.id}>
              <div><strong>{annotation.question}</strong><p>{annotation.answer}</p><small>{annotation.scope} · 阈值 {annotation.threshold.toFixed(2)} · 命中 {annotation.hit_count}</small></div>
              <button className="pill-button danger" type="button" onClick={() => void client.send('DELETE', `/operator/knowledge/annotations/${annotation.id}`).then(refresh)}>删除</button>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
