import './App.css';

import { ReadinessConsole } from './components/ReadinessConsole';
import { useReadiness } from './health/useReadiness';
import { OperatorConsole } from './operator/OperatorConsole';

function App() {
  return (
    <ReadinessConsole health={useReadiness()}>
      <OperatorConsole />
    </ReadinessConsole>
  );
}

export default App;
