
import React from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import AuthPage from './pages/AuthPage';
import AnimatedBackground from './components/AnimatedBackground';
import './index.css';

const App: React.FC = () => {
  return (
    <Router>
      <div className="relative min-h-screen">
        <AnimatedBackground />
        <div className="relative z-10">
          <Routes>
            <Route path="/auth" element={<AuthPage />} />
            <Route path="/" element={<Navigate to="/auth" replace />} />
            <Route path="*" element={<Navigate to="/auth" replace />} />
          </Routes>
        </div>
      </div>
    </Router>
  );
};

export default App;
