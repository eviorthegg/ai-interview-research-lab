import React from 'react';
import { Link } from 'react-router-dom';
import { useApi } from '../api.js';
import { Empty, ErrorBox, Loading, StatusBadge, SyntheticBadge } from '../components.jsx';

export default function Architectures() {
  const { data, error, loading, reload } = useApi('/architectures');
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data?.length) return <Empty label="No architectures. Seed the lab first." />;
  return (
    <div>
      <h2>Architecture Registry</h2>
      <table>
        <thead><tr><th>Code</th><th>Name / version</th><th>Status</th><th>Source</th>
          <th>Memory strategy</th><th>Follow-up policy</th><th>Context</th><th>Hash</th></tr></thead>
        <tbody>
          {data.map((a) => (
            <tr key={a.architecture_version_id}>
              <td>{a.code || '—'}</td>
              <td>
                <Link to={`/architectures/${a.architecture_version_id}`}>
                  {a.name} v{a.version}
                </Link>{' '}
                {a.is_protected && <StatusBadge value="protected baseline" />}
                {a.creation_source === 'seeded' && <SyntheticBadge label="seeded" />}
              </td>
              <td><StatusBadge value={a.status} /></td>
              <td>{a.creation_source}</td>
              <td>{a.components?.memory_strategy}</td>
              <td>{a.components?.follow_up_policy}</td>
              <td>{a.components?.candidate_context_strategy}</td>
              <td className="mono">{a.configuration_hash?.slice(0, 10)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
