import React, { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { apiFetch, useApi } from '../api.js';
import { Bar, Empty, ErrorBox, Loading, Money, StatusBadge, SyntheticBadge } from '../components.jsx';

function BudgetEditor({ projectId, budget, onSaved }) {
  const [open, setOpen] = React.useState(false);
  const [total, setTotal] = React.useState('');
  const [reserve, setReserve] = React.useState('');
  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (budget) {
      setTotal(String(budget.total_usd ?? ''));
      setReserve(String(budget.holdout_reserve_usd ?? ''));
    }
  }, [budget]);

  const save = async () => {
    setBusy(true); setError(null);
    try {
      await apiFetch(`/projects/${projectId}/budget`, {
        method: 'PUT',
        body: { total_usd: Number(total), holdout_reserve_usd: Number(reserve) },
      });
      setOpen(false);
      onSaved();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  if (!open) {
    return <button className="secondary" style={{ marginTop: 6 }}
      onClick={() => setOpen(true)}>Edit budget</button>;
  }
  return (
    <div style={{ marginTop: 6 }}>
      {error && <div className="warning-note">{error.message}</div>}
      <label>Total budget (USD)</label>
      <input type="number" step="0.5" min="0" value={total}
        onChange={(e) => setTotal(e.target.value)} />
      <label>Holdout reserve (USD)</label>
      <input type="number" step="0.5" min="0" value={reserve}
        onChange={(e) => setReserve(e.target.value)} />
      <div style={{ marginTop: 8 }}>
        <button onClick={save} disabled={busy}>Save</button>{' '}
        <button className="secondary" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      <div className="warning-note">Tip for cheap tests: set total to 5 and
        reserve to 1, and use a low per-experiment cost cap in the builder.</div>
    </div>
  );
}

export default function Dashboard() {
  const projects = useApi('/projects');
  const project = projects.data?.[0];
  const budget = useApi(project ? `/projects/${project.id}/budget` : null, [project?.id]);
  const experiments = useApi('/experiments');
  const pareto = useApi(project ? `/projects/${project.id}/pareto` : null, [project?.id]);
  const review = useApi(project ? `/projects/${project.id}/review-queue` : null, [project?.id]);
  const workers = useApi('/workers');
  const timeline = useApi(project ? `/projects/${project.id}/timeline` : null, [project?.id]);

  const active = useMemo(() => (experiments.data || []).filter(
    (e) => !e.is_template && !['COMPLETED', 'CANCELLED', 'FAILED', 'BUDGET_BLOCKED',
      'SAFETY_BLOCKED', 'INCONCLUSIVE', 'DRAFT'].includes(e.state)), [experiments.data]);

  if (projects.loading) return <Loading />;
  if (projects.error) return <ErrorBox error={projects.error} retry={projects.reload} />;
  if (!project) return <Empty label="No project yet. Run the seed command: python backend/seeds.py" />;

  const nonDominated = (pareto.data || []).filter((p) => !p.dominated);

  return (
    <div>
      <h2>Research Dashboard {project.is_seeded && <SyntheticBadge label="seeded project" />}</h2>
      <div className="grid cols-4">
        <div className="card"><h4>Project</h4>
          <div className="metric" style={{ fontSize: 15 }}>{project.name}</div>
          <StatusBadge value={project.status} />
          <div className="muted">Phase: {project.current_phase}</div>
        </div>
        <div className="card"><h4>Budget used</h4>
          <div className="metric"><Money usd={budget.data?.spent_usd} /></div>
          <div className="muted">of ${budget.data?.total_usd?.toFixed(2)} total</div>
          <BudgetEditor projectId={project.id} budget={budget.data}
            onSaved={budget.reload} />
        </div>
        <div className="card"><h4>Budget remaining</h4>
          <div className="metric"><Money usd={budget.data?.available_usd} /></div>
          <div className="muted">reserved: <Money usd={budget.data?.reserved_usd} /></div>
        </div>
        <div className="card"><h4>Holdout reserve</h4>
          <div className="metric"><Money usd={budget.data?.holdout_reserve_usd} /></div>
          <StatusBadge value={budget.data?.holdout_unlocked ? 'unlocked' : 'locked'} />
        </div>
      </div>

      <h3>Active experiments</h3>
      {experiments.loading ? <Loading /> : active.length === 0
        ? <Empty label="No experiment running. Launch one from Experiments." />
        : active.map((e) => (
          <div className="card" key={e.id} style={{ marginBottom: 8 }}>
            <Link to={`/experiments/${e.id}`}><b>{e.name}</b></Link>{' '}
            <StatusBadge value={e.state} />
            <span className="muted"> est. <Money usd={e.estimated_cost_usd} /> ·
              actual <Money usd={e.actual_cost_usd} /></span>
            <span style={{ float: 'right' }}>
              <button className="secondary" onClick={() =>
                apiFetch(`/experiments/${e.id}/pause`, { method: 'POST' }).then(experiments.reload)}>Pause</button>{' '}
              <button className="danger" onClick={() =>
                apiFetch(`/experiments/${e.id}/cancel`, { method: 'POST' }).then(experiments.reload)}>Cancel</button>
            </span>
          </div>
        ))}

      <h3>Best current non-dominated architectures (quality vs cost)</h3>
      {pareto.loading ? <Loading /> : nonDominated.length === 0
        ? <Empty label="No evaluated architectures yet — run an experiment to populate the Pareto frontier." />
        : (
          <div className="card">
            {nonDominated.map((p) => (
              <div key={p.architecture_version_id}>
                <Bar label={`${p.name} (±${p.quality_std}, ${p.runs} runs, $${p.cost})`}
                  value={p.quality} />
              </div>
            ))}
            <div className="warning-note">Quality = mean of evaluator metric scores
              (0–100); variance shown, not hidden. Dominated architectures excluded.</div>
          </div>
        )}

      <div className="grid cols-2" style={{ marginTop: 14 }}>
        <div className="card">
          <h4>Human-review queue</h4>
          {review.loading ? <Loading /> : (review.data || []).length === 0
            ? <div className="muted">Empty</div>
            : review.data.map((r) => (
              <div key={r.id}><StatusBadge value="needs review" /> {r.subject_type}{' '}
                <Link to={`/experiments/${r.subject_id}`}>{r.subject_id.slice(0, 8)}</Link>
                <div className="muted">{r.reason}</div></div>
            ))}
        </div>
        <div className="card">
          <h4>Worker / job activity</h4>
          {workers.loading ? <Loading /> : (workers.data || []).length === 0
            ? <div className="muted">No jobs yet</div>
            : (workers.data || []).slice(0, 6).map((j) => (
              <div key={j.id}>
                <StatusBadge value={j.status} /> {j.job_type}{' '}
                <span className="muted">{j.claimed_by || 'unclaimed'}</span>
                {j.error && <div className="warning-note">{j.error}</div>}
              </div>
            ))}
        </div>
      </div>

      <h3>Recent findings</h3>
      {timeline.loading ? <Loading /> : (timeline.data || []).length === 0
        ? <Empty />
        : (
          <div className="card">
            {(timeline.data || []).slice(0, 8).map((t) => (
              <div key={t.id} style={{ marginBottom: 4 }}>
                <span className="muted mono">{t.created_at.slice(0, 16)}</span>{' '}
                <StatusBadge value={t.event_type} /> {t.title}
              </div>
            ))}
          </div>
        )}
    </div>
  );
}
