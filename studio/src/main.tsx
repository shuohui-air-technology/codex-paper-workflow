import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

function FoundationScreen() {
  return (
    <main className="studio-foundation">
      <p className="eyebrow">PAPER WORKFLOW ORCHESTRATOR</p>
      <h1>Workflow Studio</h1>
      <p>The local workflow editor is loading.</p>
    </main>
  );
}

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('The Studio root element is missing.');
}

createRoot(rootElement).render(
  <StrictMode>
    <FoundationScreen />
  </StrictMode>,
);
