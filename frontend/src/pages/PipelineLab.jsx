import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { apiFetch, useApi, usePolling } from '../api.js';
import { Empty, ErrorBox, JsonView, Loading, Money, StatusBadge } from '../components.jsx';

const STAGE_LABELS = {
  company_research: '🌐 company research',
  field_research: '🌐 field research',
  technique_research: '🌐 technique research',
  role_brief: '🧾 role brief',
  interview_plan: '🗺 interview plan',
  question_strategist: '🎯 question strategist',
  answer_analyzer: '🔍 answer analyzer',
};

function StageChips({ stages }) {
  if (!stages?.length) return <span className="muted">core interviewer only</span>;
  return stages.map((s, i) => (
    <span key={i} className="badge info" style={{ marginRight: 4 }}>
      {STAGE_LABELS[s] || s}</span>));
}

export default function PipelineLab() {
  const projects = useApi('/projects');
  const project = projects.data?.[0];
  const tournaments = useApi('/pipeline-tournaments');
  const pipelines = useApi('/pipelines');
  const [form, setForm] = useState({
    numPipelines: 3, improveRounds: 1, budget: 3.0, mode: 'HR', allowWeb: true,
  });
  const [activeId, setActiveId] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const active = useApi(activeId ? `/pipeline-tournaments/${activeId}` : null, [activeId]);
  const isRunning = activeId && !['completed', 'cancelled', 'stopped_budget',
    'stopped_safety', 'needs_human_review', 'paused'].includes(active.data?.status);
  usePolling(active.reload, 4000, !!isRunning);
  usePolling(pipelines.reload, 6000, !!isRunning);
  usePolling(tournaments.reload, 6000, !!isRunning);

  const start = async () => {
    setBusy(true); setError(null);
    try {
      const r = await apiFetch(`/projects/${project.id}/pipeline-tournaments`, {
        method: 'POST',
        body: {
          num_pipelines: Number(form.numPipelines),
          improve_rounds: Number(form.improveRounds),
          max_budget_usd: Number(form.budget),
          interview_mode: form.mode,
          allow_web: form.allowWeb,
        },
      });
      setActiveId(r.tournament_id);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  const cancel = () => apiFetch(`/research-runs/${activeId}/cancel`, { method: 'POST' })
    .then(active.reload).catch(setError);

  if (projects.loading) return <Loading />;
  if (!project) return <Empty label="Seed the lab first (python backend/seeds.py)." />;

  const log = active.data?.log || [];
  const result = log.find((l) => l.phase === 'result');

  return (
    <div>
      <h2>Pipeline Lab — autonomous tournament</h2>
      <div className="card" style={{ marginBottom: 12 }}>
        <h4>How it works</h4>
        <div className="muted">
          1. The AI invents several different multi-stage pipelines (some can research
          the web about the company, the field, or questioning techniques).
          2. Each pipeline improves itself for a few rounds via controlled experiments.
          3. The champions compete against the baseline on the same candidates.
          The winner is the best-supported pipeline within this budget — validate it
          on regression/holdout before adopting.
        </div>
        {error && <ErrorBox error={error} />}
        <div className="grid cols-4" style={{ marginTop: 10 }}>
          <div><label>Pipelines to invent (2–5)</label>
            <input type="number" min="2" max="5" value={form.numPipelines}
              onChange={(e) => setForm({ ...form, numPipelines: e.target.value })} /></div>
          <div><label>Self-improve rounds each (0–4)</label>
            <input type="number" min="0" max="4" value={form.improveRounds}
              onChange={(e) => setForm({ ...form, improveRounds: e.target.value })} /></div>
          <div><label>Tournament budget (USD)</label>
            <input type="number" step="0.5" min="0.5" value={form.budget}
              onChange={(e) => setForm({ ...form, budget: e.target.value })} /></div>
          <div><label>Interview mode</label>
            <select value={form.mode}
              onChange={(e) => setForm({ ...form, mode: e.target.value })}>
              {['HR', 'Technical', 'HRJobDescription', 'TechnicalJobDescription',
                'InterviewToCV'].map((m) => <option key={m}>{m}</option>)}
            </select></div>
        </div>
        <label style={{ marginTop: 10 }}>
          <input type="checkbox" style={{ width: 'auto', marginRight: 6 }}
            checked={form.allowWeb}
            onChange={(e) => setForm({ ...form, allowWeb: e.target.checked })} />
          Allow web-research stages (uses WEB_RESEARCH_MODE; stub mode returns labeled
          placeholder research)
        </label>
        <div style={{ marginTop: 10 }}>
          <button onClick={start} disabled={busy || !!isRunning}>Start tournament</button>{' '}
          {isRunning && <button className="danger" onClick={cancel}>Cancel</button>}
        </div>
      </div>

      {activeId && (
        <div className="card" style={{ marginBottom: 12 }}>
          <h4>Tournament {activeId.slice(0, 8)}{' '}
            <StatusBadge value={active.data?.status || 'starting'} /></h4>
          {active.loading && !active.data ? <Loading /> : (
            <>
              <div className="mono">
                experiments: {active.data?.experiments_launched ?? 0} ·
                spent: <Money usd={active.data?.spent_usd} /> of{' '}
                <Money usd={active.data?.max_budget_usd} />
                {active.data?.stop_reason && <> · {active.data.stop_reason}</>}
              </div>
              {result && (
                <div className="state-banner ok" style={{ marginTop: 8 }}>
                  🏆 Best pipeline: <b>{result.best_pipeline}</b>{' '}
                  <Link to={`/architectures/${result.best_version_id}`}>inspect</Link>
                  <div className="warning-note">{result.note}</div>
                </div>)}
              <h4 style={{ marginTop: 10 }}>Progress log</h4>
              <div className="log">
                {log.filter((l) => l.phase).map((l, i) => (
                  <div key={i}>
                    [{l.phase}] {l.pipeline || l.challenger || ''}
                    {l.round !== undefined && ` r${l.round}`}
                    {l.delta !== undefined && ` Δ=${l.delta}`}
                    {l.label && ` (${l.label})`}
                    {l.winner && ` → winner: ${l.winner}`}
                    {l.reason && ` — ${l.reason}`}
                  </div>))}
                {!log.filter((l) => l.phase).length
                  && <div className="muted">Waiting for the worker to pick up the job…
                    (make sure worker.py is running)</div>}
              </div>
              {(active.data?.experiments || []).length > 0 && (
                <>
                  <h4 style={{ marginTop: 10 }}>Experiments in this tournament</h4>
                  <table>
                    <thead><tr><th>Name</th><th>State</th><th>Label</th><th>Cost</th></tr></thead>
                    <tbody>{active.data.experiments.map((e) => (
                      <tr key={e.id}>
                        <td><Link to={`/experiments/${e.id}`}>{e.name}</Link></td>
                        <td><StatusBadge value={e.state} /></td>
                        <td>{e.result_label ? <StatusBadge value={e.result_label} /> : '—'}</td>
                        <td><Money usd={e.actual_cost_usd} /></td>
                      </tr>))}</tbody>
                  </table>
                </>)}
            </>)}
        </div>
      )}

      <h3>All invented pipelines</h3>
      {pipelines.loading ? <Loading /> : !(pipelines.data || []).length
        ? <Empty label="No pipelines yet — start a tournament above." /> : (
          <table>
            <thead><tr><th>Pipeline</th><th>v</th><th>Status</th>
              <th>Pre-interview stages</th><th>Per-turn stages</th></tr></thead>
            <tbody>{pipelines.data.map((p) => (
              <tr key={p.architecture_version_id}>
                <td><Link to={`/architectures/${p.architecture_version_id}`}>
                  {p.name.replace('[pipeline] ', '')}</Link>
                  <div className="muted">{p.description}</div></td>
                <td>{p.version}</td>
                <td><StatusBadge value={p.status} /></td>
                <td><StageChips stages={p.pre_stages} /></td>
                <td><StageChips stages={p.per_turn_stages} /></td>
              </tr>))}</tbody>
          </table>)}

      <h3>Past tournaments</h3>
      {tournaments.loading ? <Loading /> : !(tournaments.data || []).length
        ? <Empty label="None yet." /> : (
          <table>
            <thead><tr><th>Id</th><th>Status</th><th>Experiments</th>
              <th>Budget</th><th>Result</th><th /></tr></thead>
            <tbody>{tournaments.data.map((t) => (
              <tr key={t.id}>
                <td className="mono">{t.id.slice(0, 8)}</td>
                <td><StatusBadge value={t.status} /></td>
                <td>{t.experiments_launched}</td>
                <td><Money usd={t.max_budget_usd} /></td>
                <td className="muted">{t.stop_reason}</td>
                <td><button className="secondary"
                  onClick={() => setActiveId(t.id)}>Open</button></td>
              </tr>))}</tbody>
          </table>)}
    </div>
  );
}
