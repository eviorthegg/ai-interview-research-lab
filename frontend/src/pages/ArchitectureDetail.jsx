import React, { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiFetch, useApi } from '../api.js';
import { Empty, ErrorBox, JsonView, Loading, StatusBadge } from '../components.jsx';

export default function ArchitectureDetail() {
  const { versionId } = useParams();
  const arch = useApi(`/architectures/${versionId}`, [versionId]);
  const lineage = useApi(`/architectures/${versionId}/lineage`, [versionId]);
  const [actionError, setActionError] = useState(null);
  const [actionMessage, setActionMessage] = useState(null);

  if (arch.loading) return <Loading />;
  if (arch.error) return <ErrorBox error={arch.error} retry={arch.reload} />;
  const a = arch.data;

  const act = (path, body) => {
    setActionError(null);
    apiFetch(path, { method: 'POST', body })
      .then((r) => { setActionMessage(`Status: ${r.status}`); arch.reload(); })
      .catch(setActionError);
  };

  return (
    <div>
      <h2>{a.name} <span className="muted">v{a.version}</span>{' '}
        <StatusBadge value={a.status} />
        {a.is_protected && <StatusBadge value="protected / immutable" />}
      </h2>
      {actionError && <ErrorBox error={actionError} />}
      {actionMessage && <div className="state-banner ok">{actionMessage}</div>}
      <div style={{ marginBottom: 12 }}>
        <button onClick={() => act(`/architectures/${versionId}/promote`, { reason: 'manual promotion' })}
          disabled={a.is_protected}>Promote</button>{' '}
        <button className="danger" onClick={() => act(`/architectures/${versionId}/reject`, { reason: 'manual rejection' })}
          disabled={a.is_protected}>Reject</button>
        {a.is_protected && <span className="warning-note" style={{ marginLeft: 8 }}>
          The production baseline is immutable — create descendants instead.</span>}
      </div>

      <h3>Lineage (root → this version)</h3>
      {lineage.loading ? <Loading /> : (
        <div className="card">
          {(lineage.data || []).map((n, i) => (
            <div key={n.architecture_version_id}>
              {i > 0 && <span className="muted"> ↳ </span>}
              <Link to={`/architectures/${n.architecture_version_id}`}>
                {n.name} v{n.version}</Link>{' '}
              <StatusBadge value={n.status} />
              <span className="muted"> {n.change_description}</span>
            </div>
          ))}
        </div>
      )}

      <h3>Component map</h3>
      <JsonView value={a.components} />
      <h3>Model configuration & limits</h3>
      <div className="grid cols-2">
        <JsonView value={a.model_config} />
        <JsonView value={a.limits} />
      </div>
      <h3>Prompt module versions (pinned)</h3>
      <JsonView value={a.prompt_module_versions} />
      <h3>Reproducibility metadata</h3>
      <div className="card mono">
        configuration_hash: {a.configuration_hash}<br />
        interview modes: {(a.interview_mode_support || []).join(', ')}<br />
        hypothesis: {a.hypothesis_id || '—'}<br />
        change: {a.change_description || '—'}
      </div>
      {!a.interview_mode_support?.length && <Empty label="No supported modes recorded." />}
    </div>
  );
}
