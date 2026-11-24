import React, { useState } from 'react';
import { apiFetch, useApi } from '../api.js';
import { Empty, ErrorBox, JsonView, Loading, StatusBadge, SyntheticBadge } from '../components.jsx';

export default function Personas() {
  const personas = useApi('/personas');
  const suites = useApi('/scenario-suites');
  const [selected, setSelected] = useState(null);
  const [packet, setPacket] = useState(null);
  const [hidden, setHidden] = useState(null);
  const [error, setError] = useState(null);

  const open = async (p) => {
    setSelected(p); setPacket(null); setHidden(null); setError(null);
    try {
      const data = await apiFetch(`/personas/${p.latest_version_id}`);
      setPacket(data.public_packet);
    } catch (e) { setError(e); }
  };

  const revealHidden = async () => {
    const confirmed = window.confirm(
      'WARNING: Opening hidden truth may invalidate blind human review of any '
      + 'experiment using this persona. This access is audited. Continue?');
    if (!confirmed) return;
    try {
      const data = await apiFetch(`/personas/${selected.latest_version_id}/hidden-truth`, {
        method: 'POST', body: { acknowledge: true },
      });
      setHidden(data);
    } catch (e) { setError(e); }
  };

  if (personas.loading) return <Loading />;
  if (personas.error) return <ErrorBox error={personas.error} retry={personas.reload} />;
  if (!personas.data?.length) return <Empty label="No personas. Seed the lab." />;

  return (
    <div>
      <h2>Candidate Persona Library</h2>
      {(suites.data || []).map((s) => (
        <span key={s.id} className="badge info" style={{ marginRight: 6 }}>
          {s.key}: {s.member_persona_version_ids.length} personas
          {s.locked ? ' (locked)' : ''}</span>))}
      <div className="grid cols-2" style={{ marginTop: 12 }}>
        <div>
          <table>
            <thead><tr><th>Name</th><th>Role family</th><th>Seniority</th>
              <th>Tags</th><th>Suites</th></tr></thead>
            <tbody>
              {personas.data.map((p) => (
                <tr key={p.id} onClick={() => open(p)} style={{ cursor: 'pointer' }}>
                  <td>{p.display_name} {p.is_synthetic && <SyntheticBadge />}</td>
                  <td>{p.role_family}</td>
                  <td>{p.seniority}</td>
                  <td>{(p.scenario_tags || []).map((t) => (
                    <span key={t} className="badge">{t}</span>))}</td>
                  <td>{(p.suites || []).map((s) => <StatusBadge key={s} value={s} />)}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
        <div>
          {error && <ErrorBox error={error} />}
          {selected && packet ? (
            <div className="card">
              <h4>{selected.display_name} — public packet <SyntheticBadge /></h4>
              <div className="muted">seed: {selected.seed} · version: {selected.latest_version_id?.slice(0, 8)}</div>
              <JsonView value={packet} />
              <button className="danger" onClick={revealHidden}>
                Open restricted hidden-truth view (audited)</button>
              {hidden && (
                <>
                  <div className="state-banner err">{hidden.warning}</div>
                  <JsonView value={hidden.hidden_truth} />
                </>)}
            </div>
          ) : <Empty label="Select a persona to view its public packet." />}
        </div>
      </div>
    </div>
  );
}
