import React, { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiFetch, useApi, useExperimentEvents } from '../api.js';
import { Bar, Empty, ErrorBox, JsonView, Loading, Money, StatusBadge } from '../components.jsx';

const TERMINAL = ['COMPLETED', 'CANCELLED', 'FAILED', 'BUDGET_BLOCKED',
  'SAFETY_BLOCKED', 'INCONCLUSIVE', 'NEEDS_HUMAN_REVIEW', 'DRAFT'];

function RunRow({ run, arms }) {
  const [open, setOpen] = useState(false);
  const armName = Object.entries(arms || {}).find(
    ([, v]) => v.architecture_version_id === run.architecture_version_id)?.[0] || '?';
  return (
    <>
      <tr>
        <td><button className="secondary" onClick={() => setOpen(!open)}>
          {open ? '▾' : '▸'}</button></td>
        <td>{armName}</td>
        <td>{run.mode}</td>
        <td>{run.repetition}</td>
        <td><StatusBadge value={run.status} /></td>
        <td>{run.termination_reason || '—'}</td>
        <td><Money usd={run.cost_usd} /></td>
        <td>{run.llm_calls}</td>
      </tr>
      {open && <tr><td colSpan={8}><RunDetail runId={run.id} /></td></tr>}
    </>
  );
}

function RunDetail({ runId }) {
  const transcript = useApi(`/runs/${runId}/transcript`, [runId]);
  const evals = useApi(`/runs/${runId}/evaluations`, [runId]);
  const memory = useApi(`/runs/${runId}/memory`, [runId]);
  const [tab, setTab] = useState('transcript');
  return (
    <div className="card">
      <div style={{ marginBottom: 8 }}>
        {['transcript', 'evaluations', 'deterministic', 'memory'].map((t) => (
          <button key={t} className={tab === t ? '' : 'secondary'}
            style={{ marginRight: 6 }} onClick={() => setTab(t)}>{t}</button>))}
      </div>
      {tab === 'transcript' && (transcript.loading ? <Loading /> : (
        <div className="transcript">
          {(transcript.data || []).map((t) => (
            <div className="turn" key={t.turn_index}>
              <div className="q">Q{t.turn_index}: {t.interviewer}</div>
              <div className="a">A: {t.candidate}</div>
              <div className="muted mono">topic={t.meta?.topic} follow_up={String(t.meta?.is_follow_up)}
                {' '}latency={t.latency_ms}ms cost=${(t.cost_usd || 0).toFixed(4)}</div>
            </div>))}
          {!transcript.data?.length && <Empty label="No turns stored." />}
        </div>))}
      {tab === 'evaluations' && (evals.loading ? <Loading /> : (
        <div>
          {(evals.data?.metric_scores || []).map((s, i) => (
            <Bar key={i} label={`${s.metric_key} (conf ${s.confidence}) [${s.evidence_turn_ids.join(',')}]`}
              value={s.score} />))}
          {(evals.data?.hard_constraints || []).length > 0 && (
            <div className="state-banner err">Hard-constraint findings:{' '}
              {evals.data.hard_constraints.map((h, i) => (
                <div key={i}>{h.constraint_key} ({h.source}): {h.description}</div>))}
            </div>)}
          {(evals.data?.disagreements || []).length > 0 && (
            <div className="state-banner warn">Evaluator disagreement:{' '}
              {evals.data.disagreements.map((d, i) => (
                <div key={i}>{d.metric_key}: {JSON.stringify(d.detail)}</div>))}
            </div>)}
          {!evals.data?.metric_scores?.length && <Empty label="No evaluator scores yet." />}
        </div>))}
      {tab === 'deterministic' && (evals.loading ? <Loading /> : (
        <table>
          <thead><tr><th>Metric</th><th>Value</th><th>Method</th><th>Limitations</th></tr></thead>
          <tbody>{(evals.data?.deterministic || []).map((d, i) => (
            <tr key={i}><td>{d.metric_key}</td><td className="mono">{d.value}</td>
              <td>{d.method}</td><td className="muted">{d.limitations}</td></tr>))}
          </tbody>
        </table>))}
      {tab === 'memory' && (memory.loading ? <Loading /> : (
        <div>
          {(memory.data?.events || []).map((e, i) => (
            <div key={i} className="mono">turn {e.turn_index}: {e.operation}{' '}
              {JSON.stringify(e.detail)}</div>))}
          <h3>Latest memory snapshot</h3>
          <JsonView value={(memory.data?.snapshots || []).slice(-1)[0] || {}} />
        </div>))}
    </div>
  );
}

export default function ExperimentDetail() {
  const { experimentId } = useParams();
  const experiment = useApi(`/experiments/${experimentId}`, [experimentId]);
  const runs = useApi(`/experiments/${experimentId}/runs`, [experimentId]);
  const comparison = useApi(`/experiments/${experimentId}/comparison`, [experimentId]);
  const state = experiment.data?.state;
  const live = state && !TERMINAL.includes(state);
  const { events, liveState } = useExperimentEvents(experimentId, live);
  const [actionError, setActionError] = useState(null);

  React.useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(() => { experiment.reload(); runs.reload(); }, 3000);
    return () => clearInterval(timer);
  }, [live]); // eslint-disable-line

  const act = (verb) => apiFetch(`/experiments/${experimentId}/${verb}`, { method: 'POST' })
    .then(() => { experiment.reload(); comparison.reload(); })
    .catch(setActionError);

  const cmp = comparison.data?.decision?.comparison;
  const perMetric = cmp?.per_metric || {};
  const metricKeys = useMemo(() => Object.keys(perMetric), [cmp]);

  if (experiment.loading) return <Loading />;
  if (experiment.error) return <ErrorBox error={experiment.error} retry={experiment.reload} />;
  const e = experiment.data;

  return (
    <div>
      <h2>{e.name} <StatusBadge value={liveState || e.state} /></h2>
      {actionError && <ErrorBox error={actionError} />}
      {e.state === 'BUDGET_BLOCKED' && <div className="state-banner err">
        Budget blocked — the estimate or an in-flight call exceeded a configured cap.</div>}
      {e.state === 'NEEDS_HUMAN_REVIEW' && <div className="state-banner warn">
        Needs human review — evaluator disagreement or critic challenge was preserved,
        not averaged away.</div>}
      {e.state === 'CANCELLED' && <div className="state-banner warn">
        Cancelled — all completed work and costs are preserved below.</div>}

      <div className="grid cols-4">
        <div className="card"><h4>Estimated cost</h4>
          <div className="metric"><Money usd={e.estimated_cost_usd} /></div></div>
        <div className="card"><h4>Actual cost</h4>
          <div className="metric"><Money usd={e.actual_cost_usd} /></div></div>
        <div className="card"><h4>Result label</h4>
          <div>{e.result_label ? <StatusBadge value={e.result_label} /> : '—'}</div></div>
        <div className="card"><h4>Controls</h4>
          <button className="secondary" onClick={() => act('pause')} disabled={!live}>Pause</button>{' '}
          <button className="secondary" onClick={() => act('resume')}
            disabled={e.state !== 'PAUSED'}>Resume</button>{' '}
          <button className="danger" onClick={() => act('cancel')} disabled={!live}>Cancel</button>
        </div>
      </div>

      <h3>Arms (blind labels hidden from evaluators)</h3>
      <div className="grid cols-2">
        {Object.entries(e.arms || {}).map(([arm, info]) => (
          <div className="card" key={arm}>
            <h4>{arm} {info.blind_label && <span className="badge info">label {info.blind_label}</span>}</h4>
            <Link to={`/architectures/${info.architecture_version_id}`}>
              {info.name} v{info.version}</Link>
          </div>))}
      </div>

      {live && (
        <>
          <h3>Live progress (SSE)</h3>
          <div className="log">
            {events.length === 0 && <div className="muted">Waiting for events…</div>}
            {events.map((ev, i) => (
              <div key={i}>[{ev.type}] {JSON.stringify(ev.payload || {})}</div>))}
          </div>
        </>
      )}

      <h3>Interview runs</h3>
      {runs.loading ? <Loading /> : !runs.data?.length
        ? <Empty label="No runs yet." /> : (
          <table>
            <thead><tr><th /><th>Arm</th><th>Mode</th><th>Rep</th><th>Status</th>
              <th>Termination</th><th>Cost</th><th>LLM calls</th></tr></thead>
            <tbody>{runs.data.map((r) => (
              <RunRow key={r.id} run={r} arms={e.arms} />))}</tbody>
          </table>)}

      <h3>Results comparison</h3>
      {comparison.loading ? <Loading /> : !cmp
        ? <Empty label="Comparison appears after aggregation completes." /> : (
          <>
            <div className="card">
              <h4>Overall (paired, bootstrap CI displayed — never hidden)</h4>
              <div className="mono">
                mean Δ (variant − control): {cmp.overall.mean_delta} ·
                CI [{cmp.overall.bootstrap_ci.lower}, {cmp.overall.bootstrap_ci.upper}] ·
                effect d={cmp.overall.effect_size_d} ·
                W/T/L {cmp.overall.win_tie_loss.wins}/{cmp.overall.win_tie_loss.ties}/{cmp.overall.win_tie_loss.losses}
              </div>
              <div className="muted">{cmp.overall.sample_note}</div>
              {cmp.overall.single_scenario_driver_warning
                && <div className="warning-note">Improvement may be driven by a single scenario.</div>}
              {cmp.hard_constraints && (
                <div style={{ marginTop: 6 }}>
                  Hard constraints — control: {cmp.hard_constraints.control},{' '}
                  variant: {cmp.hard_constraints.variant}
                  {cmp.hard_constraints.variant > cmp.hard_constraints.control
                    && <span className="badge err">variant regression</span>}
                </div>)}
              {cmp.efficiency && (
                <div className="mono" style={{ marginTop: 6 }}>
                  cost/run — control <Money usd={cmp.efficiency.control?.mean_cost_usd} />,
                  variant <Money usd={cmp.efficiency.variant?.mean_cost_usd} />
                  {cmp.efficiency.cost_increase_ratio !== undefined
                    && <> (Δ {Math.round(cmp.efficiency.cost_increase_ratio * 100)}%)</>}
                </div>)}
            </div>
            <h3>Per metric (control vs variant means)</h3>
            <div className="card">
              {metricKeys.map((mk) => (
                <div key={mk} style={{ marginBottom: 8 }}>
                  <Bar label={`${mk} — control`} value={perMetric[mk].control.mean ?? 0} />
                  <Bar label={`${mk} — variant (Δ ${perMetric[mk].mean_delta}, CI [${perMetric[mk].bootstrap_ci.lower}, ${perMetric[mk].bootstrap_ci.upper}])`}
                    value={perMetric[mk].variant.mean ?? 0} variant />
                </div>))}
            </div>
            {(comparison.data.pairwise || []).length > 0 && (
              <>
                <h3>Blind pairwise judging (both orderings)</h3>
                <div className="card">
                  {comparison.data.pairwise.map((p, i) => (
                    <div key={i} className="mono">ordering {p.ordering}: winner{' '}
                      {p.output.overall_winner} (conf {p.output.confidence})</div>))}
                </div>
              </>)}
            {(comparison.data.disagreements || []).length > 0 && (
              <div className="state-banner warn">
                {comparison.data.disagreements.length} evaluator disagreement(s) preserved.
              </div>)}
            {(comparison.data.critic_reviews || []).length > 0 && (
              <>
                <h3>Critic review</h3>
                <JsonView value={comparison.data.critic_reviews} />
              </>)}
            <h3>Decision</h3>
            <JsonView value={comparison.data.decision?.decision || {}} />
          </>)}
    </div>
  );
}
