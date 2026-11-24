// API client for /api/v1/research-lab. Every displayed value comes from here —
// nothing is fabricated in the UI.
import { useCallback, useEffect, useRef, useState } from 'react';

const BASE = '/api/v1/research-lab';

export async function apiFetch(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    const message = payload?.errors?.[0]?.message || `HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.code = payload?.errors?.[0]?.code;
    throw error;
  }
  return payload?.data;
}

// Small fetch hook — background-refresh aware.
// KEY FIX: reload() only sets loading=true on the very first fetch (no data yet).
// Subsequent reloads silently update data in the background — no flicker, no
// blink, no UI state reset. Open modals/accordions stay open.
export function useApi(path, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(!!path); // true only if we have a path to fetch
  const hasData = useRef(false);

  const reload = useCallback(() => {
    if (!path) return;
    // Only show the loading spinner on the very first load (no data yet).
    // Background refreshes are completely silent — data swaps in without flicker.
    if (!hasData.current) setLoading(true);
    setError(null);
    apiFetch(path)
      .then((d) => {
        hasData.current = true;
        setData(d);
      })
      .catch((e) => {
        // On error, only show it if we have no data to fall back on
        if (!hasData.current) setError(e);
      })
      .finally(() => setLoading(false));
  }, [path]); // eslint-disable-line

  useEffect(() => {
    hasData.current = false; // reset when path changes
    setData(null);
    setError(null);
    setLoading(!!path);
    reload();
  }, [reload, ...deps]); // eslint-disable-line

  return { data, error, loading, reload };
}

// Clean polling hook — auto-refreshes the given reload fn at an interval.
// Stops when the component unmounts or when `active` becomes false.
export function usePolling(reloadFn, intervalMs = 3000, active = true) {
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(reloadFn, intervalMs);
    return () => clearInterval(t);
  }, [reloadFn, intervalMs, active]);
}

// SSE hook with polling fallback.
export function useExperimentEvents(experimentId, enabled) {
  const [events, setEvents] = useState([]);
  const [state, setState] = useState(null);
  const sourceRef = useRef(null);
  useEffect(() => {
    if (!experimentId || !enabled) return undefined;
    setEvents([]);
    let poller = null;
    try {
      const source = new EventSource(`${BASE}/experiments/${experimentId}/events`);
      sourceRef.current = source;
      const onAny = (e) => {
        try {
          const parsed = JSON.parse(e.data);
          setState(parsed.state);
          setEvents((prev) => [...prev.slice(-199), { type: e.type, ...parsed }]);
        } catch { /* heartbeat */ }
      };
      ['state_change', 'interview_started', 'interview_completed', 'done', 'message']
        .forEach((t) => source.addEventListener(t, onAny));
      source.onerror = () => {
        source.close();
        // polling fallback
        let after = 0;
        poller = setInterval(async () => {
          try {
            const rows = await apiFetch(`/experiments/${experimentId}/progress?after=${after}`);
            if (rows.length) {
              after = rows[rows.length - 1].seq;
              setEvents((prev) => [...prev.slice(-199),
                ...rows.map((r) => ({ type: r.event_type, payload: r.payload }))]);
            }
          } catch { /* keep polling */ }
        }, 2000);
      };
      return () => { source.close(); if (poller) clearInterval(poller); };
    } catch {
      return undefined;
    }
  }, [experimentId, enabled]);
  return { events, liveState: state };
}
