import React, { useState } from 'react';
import { apiFetch, useApi } from '../api.js';
import { Empty, ErrorBox, Loading, StatusBadge } from '../components.jsx';

export default function Prompts() {
  const prompts = useApi('/prompts');
  const [selected, setSelected] = useState(null); // {moduleId, versionId}
  const [content, setContent] = useState(null);
  const [diff, setDiff] = useState(null);
  const [draft, setDraft] = useState('');
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);

  const open = async (moduleId, versionId) => {
    setSelected({ moduleId, versionId });
    setDiff(null); setMessage(null); setError(null);
    try {
      const v = await apiFetch(`/prompts/${moduleId}/versions/${versionId}`);
      setContent(v);
      setDraft(v.template_text);
    } catch (e) { setError(e); }
  };

  const showDiff = async () => {
    try {
      const d = await apiFetch(`/prompts/${selected.moduleId}/diff/${selected.versionId}`);
      setDiff(d.diff || '(no previous version)');
    } catch (e) { setError(e); }
  };

  const saveAsNew = async () => {
    setError(null);
    try {
      const r = await apiFetch(`/prompts/${selected.moduleId}/versions`, {
        method: 'POST', body: { template_text: draft, created_by: 'human' },
      });
      setMessage(`Saved as new version v${r.version} (published versions are immutable).`);
      prompts.reload();
    } catch (e) { setError(e); }
  };

  const restore = async () => {
    setError(null);
    try {
      const r = await apiFetch(`/prompts/${selected.moduleId}/restore`, {
        method: 'POST', body: { version_id: selected.versionId },
      });
      setMessage(`Restored as new version v${r.version}.`);
      prompts.reload();
    } catch (e) { setError(e); }
  };

  if (prompts.loading) return <Loading />;
  if (prompts.error) return <ErrorBox error={prompts.error} retry={prompts.reload} />;
  if (!prompts.data?.length) return <Empty label="No prompt modules. Seed the lab." />;

  return (
    <div>
      <h2>Prompt & Component Editor</h2>
      {error && <ErrorBox error={error} />}
      {message && <div className="state-banner ok">{message}</div>}
      <div className="grid cols-2">
        <div>
          {prompts.data.map((m) => (
            <div className="card" key={m.id} style={{ marginBottom: 8 }}>
              <b>{m.key}</b> <span className="muted">({m.category})</span>{' '}
              {m.is_protected && <StatusBadge value="protected" />}
              <div>
                {m.versions.map((v) => (
                  <button key={v.id} className="secondary" style={{ margin: '4px 4px 0 0' }}
                    onClick={() => open(m.id, v.id)}>
                    v{v.version} ({v.status}, {v.created_by})
                  </button>))}
              </div>
              {m.is_protected && <div className="warning-note">
                Protected safety/isolation module — autonomous agents cannot edit it.</div>}
            </div>))}
        </div>
        <div>
          {content ? (
            <div className="card">
              <h4>Version v{content.version} <StatusBadge value={content.status} />
                <span className="mono muted"> {content.content_hash?.slice(0, 12)}</span></h4>
              <textarea rows={16} value={draft} onChange={(e) => setDraft(e.target.value)} />
              <div style={{ marginTop: 8 }}>
                <button onClick={saveAsNew}>Save as new version</button>{' '}
                <button className="secondary" onClick={restore}>Restore this as new version</button>{' '}
                <button className="secondary" onClick={showDiff}>Diff vs previous</button>
              </div>
              {diff && <pre className="json">{diff}</pre>}
            </div>
          ) : <Empty label="Select a prompt version to inspect." />}
        </div>
      </div>
    </div>
  );
}
