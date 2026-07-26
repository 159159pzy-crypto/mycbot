import './App.css';

import { Console } from './console/Console';
import { useReadiness } from './health/useReadiness';

function App() {
  return <Console health={useReadiness()} />;
}

export default App;
