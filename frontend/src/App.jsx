import React from 'react';
import { NavLink, Route, Routes } from 'react-router-dom';
import Dashboard from './pages/Dashboard.jsx';
import Architectures from './pages/Architectures.jsx';
import ArchitectureDetail from './pages/ArchitectureDetail.jsx';
import Experiments from './pages/Experiments.jsx';
import ExperimentDetail from './pages/ExperimentDetail.jsx';
import Prompts from './pages/Prompts.jsx';
import Personas from './pages/Personas.jsx';
import Timeline from './pages/Timeline.jsx';
import FinalReport from './pages/FinalReport.jsx';
import PipelineLab from './pages/PipelineLab.jsx';

const LINKS = [
  ['/', 'Dashboard'],
  ['/architectures', 'Architectures'],
  ['/experiments', 'Experiments'],
  ['/prompts', 'Prompts'],
  ['/personas', 'Personas'],
  ['/pipeline-lab', 'Pipeline Lab'],
  ['/timeline', 'Timeline'],
  ['/final-report', 'Final Report'],
];

export default function App() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <h1><span>Evior</span> Interview Research Lab</h1>
        <nav>
          {LINKS.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === '/'}
              className={({ isActive }) => (isActive ? 'active' : '')}>
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/architectures" element={<Architectures />} />
          <Route path="/architectures/:versionId" element={<ArchitectureDetail />} />
          <Route path="/experiments" element={<Experiments />} />
          <Route path="/experiments/:experimentId" element={<ExperimentDetail />} />
          <Route path="/prompts" element={<Prompts />} />
          <Route path="/personas" element={<Personas />} />
          <Route path="/pipeline-lab" element={<PipelineLab />} />
          <Route path="/timeline" element={<Timeline />} />
          <Route path="/final-report" element={<FinalReport />} />
        </Routes>
      </main>
    </div>
  );
}
