import React, { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { apiFetch, useApi } from '../api.js';
import { Empty, ErrorBox, Loading, Money, StatusBadge, SyntheticBadge } from '../components.jsx';

export default function Experiments() {
  const experiments = useApi('/experiments');
  const architectures = useApi('/architectures');
  const personas = useApi('/personas');
  const projects = useApi('/projects');
  const navigate = useNavigate();

  const [form, setForm] = useState({
    name: '', control: '', variant: '', modes: ['HR'], personaIds: [],
    repetitions: 2, maxCost: 3.0, maxTurns: 6, independent: '',
  });
  const [estimate, setEstimate] = useState(null);
  const [createdId, setCreatedId] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const templates = useMemo(() => (experiments.data || []).filter((e) => e.is_template), [experiments.data]);
  const real = useMemo(() => (experiments.data || []).filter((e) => !e.is_template), [experiments.data]);

  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }));

  const cloneTemplate = (t) => {
    set('name', `${t.name} (run)`);
    setForm((f) => ({
      ...f,
      name: `${t.name} (run)`,
      control: t.spec.control_architecture_version_id,
      variant: t.spec.variant_architecture_version_id,
      modes: t.spec.interview_modes,
      personaIds: t.spec.scenario_persona_version_ids,
      repetitions: t.spec.repetitions,
      maxCost: t.spec.max_cost_usd,
      independent: t.spec.independent_variable,
    }));
    window.scrollTo(0, 0);
  };

  const buildDesign = () => ({
    research_question: form.name,
    hypothesis_id: '',
    control_architecture_version_id: form.control,
    variant_architecture_version_id: form.variant,
    independent_variable: form.independent || 'unspecified',
    dependent_metrics: ['question_relevance', 'follow_up_depth', 'evidence_quality',
      'adaptation_to_answers', 'hallucination_avoidance'],
    scenario_persona_version_ids: form.personaIds,
    interview_modes: form.modes,
    repetitions: Number(form.repetitions),
    max_cost_usd: Number(form.maxCost),
    rubric_version_id: '',
    model_config_lock: { max_turns: Number(form.maxTurns) },
  });

  const createAndEstimate = async () => {
    setBusy(true); setError(null); setEstimate(null);
    try {
      const created = await apiFetch('/experiments', {
        method: 'POST',
        body: { name: form.name || 'Untitled experiment', design: buildDesign() },
      });
      setCreatedId(created.id);
      const est = await apiFetch(`/experiments/${created.id}/estimate`, { method: 'POST' });
      setEstimate(est);
      experiments.reload();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  const launch = async () => {
    setBusy(true); setError(null);
    try {
      await apiFetch(`/experiments/${createdId}/launch`, {
        method: 'POST',
        headers: { 'Idempotency-Key': `ui-${createdId}` },
      });
      navigate(`/experiments/${createdId}`);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  if (experiments.loading || architectures.loading || personas.loading) return <Loading />;
  if (experiments.error) return <ErrorBox error={experiments.error} retry={experiments.reload} />;
  if (!projects.data?.length) return <Empty label="Seed the lab first (python backend/seeds.py)." />;

  const archOptions = (architectures.data || []);

  return (
    <div>
      <h2>Experiment Builder</h2>
      {error && <ErrorBox error={error} />}
      <div className="card">
        <div className="grid cols-2">
          <div>
            <label>Experiment name / research question</label>
            <input value={form.name} onChange={(e) => set('name', e.target.value)} />
            <label>Control architecture</label>
            <select value={form.control} onChange={(e) => set('control', e.target.value)}>
              <option value="">— select —</option>
              {archOptions.map((a) => (
                <option key={a.architecture_version_id} value={a.architecture_version_id}>
                  {a.code} {a.name} v{a.version}</option>))}
            </select>
            <label>Variant architecture</label>
            <select value={form.variant} onChange={(e) => set('variant', e.target.value)}>
              <option value="">— select —</option>
              {archOptions.map((a) => (
                <option key={a.architecture_version_id} value={a.architecture_version_id}>
                  {a.code} {a.name} v{a.version}</option>))}
            </select>
            <label>Independent variable (the ONE thing that differs)</label>
            <input value={form.independent} placeholder="e.g. candidate_context_strategy"
              onChange={(e) => set('independent', e.target.value)} />
          </div>
          <div>
            <label>Interview modes</label>
            <select multiple value={form.modes} size={5}
              onChange={(e) => set('modes', [...e.target.selectedOptions].map((o) => o.value))}>
              {['HR', 'Technical', 'HRJobDescription', 'TechnicalJobDescription',
                'CompanySpecificHR', 'CompanySpecificTechnical', 'InterviewToCV']
                .map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            <label>Candidate scenarios (personas)</label>
            <select multiple value={form.personaIds} size={5}
              onChange={(e) => set('personaIds', [...e.target.selectedOptions].map((o) => o.value))}>
              {(personas.data || []).map((p) => (
                <option key={p.latest_version_id} value={p.latest_version_id}>
                  {p.display_name} — {p.role_family} ({(p.suites || []).join(',')})</option>))}
            </select>
            <div className="grid cols-3">
              <div><label>Repetitions</label>
                <input type="number" min="1" max="10" value={form.repetitions}
                  onChange={(e) => set('repetitions', e.target.value)} /></div>
              <div><label>Cost cap (USD)</label>
                <input type="number" step="0.5" value={form.maxCost}
                  onChange={(e) => set('maxCost', e.target.value)} /></div>
              <div><label>Turn cap</label>
                <input type="number" min="2" max="20" value={form.maxTurns}
                  onChange={(e) => set('maxTurns', e.target.value)} /></div>
            </div>
          </div>
        </div>
        <div style={{ marginTop: 12 }}>
          <button onClick={createAndEstimate}
            disabled={busy || !form.control || !form.variant || !form.personaIds.length}>
            Validate & estimate cost
          </button>{' '}
          {estimate && (
            <>
              <span className="mono"> estimate: <Money usd={estimate.estimated_total_usd} />{' '}
                ({estimate.interview_runs} interview runs · <Money usd={estimate.per_run_usd} />/run)</span>{' '}
              <button className="btn" onClick={launch} disabled={busy}>Launch</button>
            </>
          )}
        </div>
        {estimate && <div className="warning-note">Estimated cost from the versioned
          pricing table — actuals are reconciled per call and shown live.</div>}
      </div>

      <h3>Seed experiment templates (E1–E12) <SyntheticBadge label="seeded" /></h3>
      <table>
        <thead><tr><th>Code</th><th>Name</th><th>Modes</th><th>Reps</th><th>Cap</th><th /></tr></thead>
        <tbody>
          {templates.map((t) => (
            <tr key={t.id}>
              <td>{t.code}</td><td>{t.name}</td>
              <td>{(t.spec.interview_modes || []).join(', ')}</td>
              <td>{t.spec.repetitions}</td>
              <td><Money usd={t.spec.max_cost_usd} /></td>
              <td><button className="secondary" onClick={() => cloneTemplate(t)}>
                Load into builder</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Experiment runs</h3>
      {real.length === 0 ? <Empty label="No experiments launched yet." /> : (
        <table>
          <thead><tr><th>Name</th><th>State</th><th>Label</th><th>Estimated</th>
            <th>Actual</th><th>Created</th></tr></thead>
          <tbody>
            {real.map((e) => (
              <tr key={e.id}>
                <td><Link to={`/experiments/${e.id}`}>{e.name}</Link></td>
                <td><StatusBadge value={e.state} /></td>
                <td>{e.result_label ? <StatusBadge value={e.result_label} /> : '—'}</td>
                <td><Money usd={e.estimated_cost_usd} /></td>
                <td><Money usd={e.actual_cost_usd} /></td>
                <td className="muted">{e.created_at.slice(0, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
