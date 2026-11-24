import React, { useState } from 'react';
import { apiFetch, useApi } from '../api.js';
import { Bar, Empty, ErrorBox, JsonView, Loading, Money, StatusBadge } from '../components.jsx';

export default function FinalReport() {
  const projects = useApi('/projects');
  const project = projects.data?.[0];
  const report = useApi(project ? `/projects/${project.id}/final-report` : null, [project?.id]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const generate = async () => {
    setBusy(true); setError(null);
    try {
      await apiFetch(`/projects/${project.id}/final-report`, { method: 'POST' });
      report.reload();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  const unlockHoldout = async () => {
    if (!window.confirm('Unlock the holdout suite and reserve for final validation? '
      + 'Finalists and rubrics should be frozen first.')) return;
    try {
      await apiFetch(`/projects/${project.id}/final-validation`, { method: 'POST' });
      report.reload();
    } catch (e) { setError(e); }
  };

  if (projects.loading) return <Loading />;
  if (!project) return <Empty label="Seed the lab first." />;
  const r = report.data?.report;

  return (
    <div>
      <h2>Final Recommendation</h2>
      {error && <ErrorBox error={error} />}
      <div style={{ marginBottom: 12 }}>
        <button className="secondary" onClick={unlockHoldout}>
          Begin final validation (unlock holdout)</button>{' '}
        <button onClick={generate} disabled={busy}>Generate final report</button>
      </div>
      {report.error && report.error.status === 404
        && <Empty label="No final report generated yet." />}
      {report.error && report.error.status !== 404 && <ErrorBox error={report.error} />}
      {r && (
        <>
          <div className="state-banner">Status: <StatusBadge value={report.data.status} />
            <span className="muted" style={{ marginLeft: 8 }}>
              generated {report.data.created_at?.slice(0, 19)}</span></div>
          <div className="card"><h4>Scope</h4>
            <div className="warning-note">{r.scope?.note}</div>
            <div className="mono">experiments: {r.scope?.experiments_run} ·
              completed: {r.scope?.experiments_completed} ·
              hypotheses: {r.scope?.hypotheses_tested} ·
              holdout validated: {String(r.scope?.holdout_validated)}</div>
            <div className="muted">Baseline: {r.baseline}</div>
          </div>

          <h3>Recommended overall</h3>
          {r.recommended_overall ? (
            <div className="card">
              <b>{r.recommended_overall.name}</b>{' '}
              <StatusBadge value={r.recommended_overall.status} />
              <div className="mono">quality {r.recommended_overall.quality}
                (±{r.recommended_overall.quality_std}) ·
                cost/run <Money usd={r.recommended_overall.cost} /> ·
                {r.recommended_overall.runs} runs ·
                unresolved hard constraints: {r.recommended_overall.unresolved_hard_constraints}</div>
            </div>
          ) : <Empty label="No architecture met the recommendation bar." />}

          <h3>Recommended by interview mode</h3>
          {Object.keys(r.recommended_by_mode || {}).length
            ? <JsonView value={r.recommended_by_mode} />
            : <Empty label="Not enough per-mode evidence." />}

          <h3>Pareto frontier (quality vs cost)</h3>
          <div className="card">
            {(r.pareto_frontier || []).map((p) => (
              <Bar key={p.architecture_version_id}
                label={`${p.name}${p.dominated ? ' (dominated)' : ''} — $${p.cost}/run`}
                value={p.quality} variant={p.dominated} />))}
            {!(r.pareto_frontier || []).length && <div className="muted">No data.</div>}
          </div>

          <h3>Quantitative findings</h3>
          <JsonView value={r.quantitative_findings} />
          <h3>Costs</h3>
          <div className="card mono">
            spent <Money usd={r.costs?.total_spent_usd} /> of{' '}
            <Money usd={r.costs?.total_budget_usd} /> (holdout reserve{' '}
            <Money usd={r.costs?.holdout_reserve_usd} />)</div>
          <h3>Failed approaches</h3>
          {(r.failed_approaches || []).length
            ? <JsonView value={r.failed_approaches} /> : <Empty label="None recorded." />}
          <h3>Limitations</h3>
          <ul>{(r.limitations || []).map((l, i) => <li key={i}>{l}</li>)}</ul>
          <h3>Production safeguards</h3>
          <ul>{(r.production_safeguards || []).map((l, i) => <li key={i}>{l}</li>)}</ul>
          <h3>Next experiments</h3>
          <ul>{(r.next_experiments || []).map((l, i) => <li key={i}>{l}</li>)}</ul>
        </>)}
    </div>
  );
}
