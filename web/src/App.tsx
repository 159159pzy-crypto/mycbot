import './App.css';

import { ReadinessConsole } from './components/ReadinessConsole';
import { useReadiness } from './health/useReadiness';

function App() {
  return <ReadinessConsole health={useReadiness()} />;
}

export default App;
