import React, { useState } from 'react';
import { apiFetch, useApi, usePolling } from '../api.js';
import { Empty, ErrorBox, JsonView, Loading, StatusBadge } from '../components.jsx';

export default function Timeline() {
  const projects = useApi('/projects');
  const project = projects.data?.[0];
  const timeline = useApi(project ? `/projects/${project.id}/timeline` : null, [project?.id]);
  const hypotheses = useApi('/hypotheses');
  const [researchRun, setResearchRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [maxRounds, setMaxRounds] = useState(2);
  const [maxBudgetUsd, setMaxBudgetUsd] = useState(2.0);
  const [editingBudget, setEditingBudget] = useState(false);
  const [newTotalBudget, setNewTotalBudget] = useState('');
  const [budgetBusy, setBudgetBusy] = useState(false);
  const [budgetError, setBudgetError] = useState(null);

  const budget = useApi(project ? `/projects/${project.id}/budget` : null, [project?.id]);

  React.useEffect(() => {
    if (budget.data) {
      setNewTotalBudget(String(budget.data.total_usd ?? ''));
    }
  }, [budget.data]);

  const saveBudget = async () => {
    setBudgetBusy(true); setBudgetError(null);
    try {
      await apiFetch(`/projects/${project.id}/budget`, {
        method: 'PUT',
        body: { total_usd: Number(newTotalBudget) },
      });
      budget.reload();
      setEditingBudget(false);
    } catch (e) {
      setBudgetError(e);
    } finally {
      setBudgetBusy(false);
    }
  };

  const startCycle = async () => {
    setBusy(true); setError(null);
    try {
      const r = await apiFetch(`/projects/${project.id}/research-runs`, {
        method: 'POST', body: { max_rounds: maxRounds, max_budget_usd: maxBudgetUsd },
      });
      setResearchRun(r.research_run_id);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  const rr = useApi(researchRun ? `/research-runs/${researchRun}` : null, [researchRun]);
  const isRunning = researchRun && !['completed', 'cancelled', 'stopped_budget',
    'stopped_safety', 'needs_human_review'].includes(rr.data?.status);

  // Silent background polling — no page blink, no spinner flash
  usePolling(rr.reload, 4000, isRunning);
  usePolling(timeline.reload, 5000, isRunning);


  if (projects.loading) return <Loading />;
  if (!project) return <Empty label="Seed the lab first." />;

  return (
    <div>
      <h2>Research Timeline</h2>
      {error && <ErrorBox error={error} />}
      <div className="card" style={{ marginBottom: 12 }}>
        <h4>Bounded autonomous research cycle</h4>
        
        <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap', marginTop: 10, marginBottom: 15 }}>
          <div>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: 'bold' }}>Max Rounds</label>
            <input 
              type="number" 
              min="1" 
              max="20" 
              value={maxRounds} 
              onChange={(e) => setMaxRounds(Number(e.target.value))} 
              style={{ width: '80px', padding: '4px', marginTop: '4px' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: 'bold' }}>Max Cycle Budget (USD)</label>
            <input 
              type="number" 
              step="0.5" 
              min="0.5" 
              max="50" 
              value={maxBudgetUsd} 
              onChange={(e) => setMaxBudgetUsd(Number(e.target.value))} 
              style={{ width: '100px', padding: '4px', marginTop: '4px' }}
            />
          </div>
          
          <div style={{ borderLeft: '1px solid #ddd', paddingLeft: '20px' }}>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: 'bold' }}>
              Project Budget: {budget.loading ? '...' : `$${budget.data?.total_usd?.toFixed(2)}`} 
              <span style={{ fontSize: '11px', color: '#666', fontWeight: 'normal', marginLeft: 8 }}>
                (Remaining: ${budget.data?.available_usd?.toFixed(2)})
              </span>
            </label>
            {editingBudget ? (
              <div style={{ marginTop: 4, display: 'flex', gap: 6, alignItems: 'center' }}>
                <input 
                  type="number" 
                  step="1" 
                  min="0" 
                  value={newTotalBudget} 
                  onChange={(e) => setNewTotalBudget(e.target.value)} 
                  style={{ width: '80px', padding: '4px' }}
                />
                <button onClick={saveBudget} disabled={budgetBusy} style={{ padding: '4px 8px', fontSize: '12px' }}>Save</button>
                <button className="secondary" onClick={() => setEditingBudget(false)} style={{ padding: '4px 8px', fontSize: '12px' }}>Cancel</button>
                {budgetError && <div className="warning-note" style={{ margin: 0, padding: '2px 6px', fontSize: '11px' }}>{budgetError.message}</div>}
              </div>
            ) : (
              <button 
                className="secondary" 
                onClick={() => setEditingBudget(true)}
                style={{ padding: '4px 8px', fontSize: '12px', marginTop: 4 }}
              >
                Edit Project Budget
              </button>
            )}
          </div>
        </div>

        <div className="muted">
          Autonomous run will execute at most <b>{maxRounds} rounds</b> with a budget cap of <b>${maxBudgetUsd}</b>.
          Stops on safety failures, budget depletion, or if no improvement is found.
        </div>
        
        <div style={{ marginTop: 12 }}>
          <button onClick={startCycle} disabled={busy || (rr.data && rr.data.status === 'running')}>
            Start autonomous cycle</button>{' '}
          {researchRun && rr.data && (
            <>
              <StatusBadge value={rr.data.status} />{' '}
              <span className="mono">rounds {rr.data.rounds_completed}/{rr.data.max_rounds} ·
                experiments {rr.data.experiments_launched}</span>
              {rr.data.status === 'running' && (
                <button className="danger" style={{ marginLeft: 8 }} onClick={() =>
                  apiFetch(`/research-runs/${researchRun}/cancel`, { method: 'POST' })
                    .then(rr.reload)}>Cancel</button>)}
              {rr.data.stop_reason && <div className="muted">stop: {rr.data.stop_reason}</div>}
              {rr.data.log?.length > 0 && <JsonView value={rr.data.log} />}
            </>)}
        </div>
      </div>

      <h3>Hypotheses</h3>
      {hypotheses.loading ? <Loading /> : !hypotheses.data?.length
        ? <Empty label="No hypotheses yet." /> : (
          <table>
            <thead><tr><th>Question</th><th>Statement</th><th>Variable</th>
              <th>Status</th><th>By</th></tr></thead>
            <tbody>{hypotheses.data.map((h) => (
              <tr key={h.id}><td>{h.research_question}</td><td>{h.statement}</td>
                <td className="mono">{h.independent_variable}</td>
                <td><StatusBadge value={h.status} /></td><td>{h.created_by}</td></tr>))}
            </tbody>
          </table>)}

      <h3>Events</h3>
      {timeline.loading ? <Loading /> : !timeline.data?.length ? <Empty /> : (
        <div className="card">
          {timeline.data.map((t) => (
            <div key={t.id} style={{ marginBottom: 6 }}>
              <span className="muted mono">{t.created_at.slice(0, 19)}</span>{' '}
              <StatusBadge value={t.event_type} /> <b>{t.title}</b>
              <span className="muted"> — {t.actor}</span>
            </div>))}
        </div>)}
    </div>
  );
}
