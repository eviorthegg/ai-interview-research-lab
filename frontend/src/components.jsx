// Shared UI states: loading / empty / error / partial — required on every screen.
import React from 'react';

export function Loading({ label = 'Loading…' }) {
  return <div className="empty" role="status">{label}</div>;
}

export function ErrorBox({ error, retry }) {
  return (
    <div className="state-banner err" role="alert">
      Error: {error?.message || String(error)}
      {retry && <button className="secondary" style={{ marginLeft: 10 }} onClick={retry}>Retry</button>}
    </div>
  );
}

export function Empty({ label = 'Nothing here yet.' }) {
  return <div className="empty">{label}</div>;
}

const STATE_TONE = {
  COMPLETED: 'ok', RUNNING_INTERVIEWS: 'info', QUEUED: 'info', PREPARING: 'info',
  RUNNING_DETERMINISTIC_ANALYSIS: 'info', RUNNING_EVALUATORS: 'info',
  AGGREGATING: 'info', CRITIC_REVIEW: 'info', DECIDING: 'info',
  CANCELLED: 'warn', CANCEL_REQUESTED: 'warn', PAUSED: 'warn',
  NEEDS_HUMAN_REVIEW: 'warn', INCONCLUSIVE: 'warn',
  FAILED: 'err', BUDGET_BLOCKED: 'err', SAFETY_BLOCKED: 'err',
  promoted: 'ok', active: 'info', draft: 'warn', rejected: 'err',
  safety_rejected: 'err', retired: 'warn',
  completed: 'ok', in_progress: 'info', pending: 'warn', failed: 'err',
  budget_blocked: 'err', cancelled: 'warn',
};

export function StatusBadge({ value }) {
  if (!value) return null;
  return <span className={`badge ${STATE_TONE[value] || 'info'}`}>{value}</span>;
}

export function SyntheticBadge({ label = 'synthetic' }) {
  return <span className="badge synthetic">{label}</span>;
}

export function JsonView({ value }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Bar({ label, value, max = 100, variant = false }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="bar-row">
      <div className="bar-label" title={label}>{label}</div>
      <div className="bar-track"><div className={`bar-fill${variant ? ' variant' : ''}`} style={{ width: `${pct}%` }} /></div>
      <div className="bar-value">{typeof value === 'number' ? value.toFixed(1) : value}</div>
    </div>
  );
}

export function Money({ usd }) {
  if (usd === null || usd === undefined) return <span className="muted">—</span>;
  return <span className="mono">${Number(usd).toFixed(4)}</span>;
}
