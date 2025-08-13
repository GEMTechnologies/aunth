import React, { useState, useEffect } from 'react';
import AuthPage from './pages/AuthPage';
import ProfilePage from './pages/ProfilePage';
import OrganizationPage from './pages/OrganizationPage';
import SecurityPage from './pages/SecurityPage';
import { apiMe } from './lib/api';
import './index.css';

// Assume these components are defined elsewhere and handle context selection and routing
import ContextRouter from './components/ContextRouter'; // Placeholder for ContextRouter component
import ContextPicker from './components/ContextPicker'; // Placeholder for ContextPicker component

type Page = 'auth' | 'profile' | 'organizations' | 'security';

interface User {
  id: string;
  display_name: string;
  avatar_url?: string;
  locale: string;
  status: string;
  primary_email?: {
    email: string;
    is_verified: boolean;
  };
}

function App() {
  const [currentPage, setCurrentPage] = useState<Page>('auth');
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [currentContext, setCurrentContext] = useState<any>(null);
  const [showContextRouter, setShowContextRouter] = useState(false);

  useEffect(() => {
    checkAuth();
  }, []);

  const checkAuth = async () => {
    try {
      const token = localStorage.getItem('access_token');
      if (!token) {
        setLoading(false);
        return;
      }

      const userData = await apiMe();
      setUser(userData);
      setCurrentPage('profile');
    } catch (error) {
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
      console.error('Auth check failed:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleLogin = (userData: User) => {
    setUser(userData);
    setShowContextRouter(true);
  };

  const handleLogout = () => {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    setUser(null);
    setCurrentPage('auth');
    setCurrentContext(null); // Reset context on logout
    setShowContextRouter(false); // Hide context router on logout
  };

  const handleContextResolved = (context: any) => {
    setCurrentContext(context);
    setShowContextRouter(false);
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-blue-50 via-indigo-50 to-purple-50 flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto"></div>
          <p className="mt-4 text-gray-600">Loading...</p>
        </div>
      </div>
    );
  }

  if (!user) {
    return <AuthPage onLogin={handleLogin} />;
  }

  // Render ContextPicker if user is logged in and context needs to be selected
  if (showContextRouter && user) {
    return <ContextPicker user={user} onContextResolved={handleContextResolved} />;
  }

  // Render main application if context is resolved or not needed
  return (
    <div className="min-h-screen bg-gray-50">
      {/* Navigation */}
      <nav className="bg-white shadow-sm border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16">
            <div className="flex items-center">
              <h1 className="text-xl font-bold text-gray-900">Granada Auth</h1>
            </div>
            <div className="flex items-center space-x-4">
              <button
                onClick={() => setCurrentPage('profile')}
                className={`px-3 py-2 text-sm font-medium rounded-md ${
                  currentPage === 'profile'
                    ? 'bg-blue-100 text-blue-700'
                    : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                Profile
              </button>
              <button
                onClick={() => setCurrentPage('organizations')}
                className={`px-3 py-2 text-sm font-medium rounded-md ${
                  currentPage === 'organizations'
                    ? 'bg-blue-100 text-blue-700'
                    : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                Organizations
              </button>
              <button
                onClick={() => setCurrentPage('security')}
                className={`px-3 py-2 text-sm font-medium rounded-md ${
                  currentPage === 'security'
                    ? 'bg-blue-100 text-blue-700'
                    : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                Security
              </button>
              <button
                onClick={handleLogout}
                className="bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium hover:bg-red-700"
              >
                Logout
              </button>
            </div>
          </div>
        </div>
      </nav>

      {/* Page Content */}
      <main className="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        {/* Render ContextRouter if a context is selected, otherwise render the specific page */}
        {currentContext ? (
          <ContextRouter currentContext={currentContext} user={user} />
        ) : (
          <>
            {currentPage === 'profile' && <ProfilePage user={user} onUserUpdate={setUser} />}
            {currentPage === 'organizations' && <OrganizationPage />}
            {currentPage === 'security' && <SecurityPage user={user} />}
          </>
        )}
      </main>
    </div>
  );
}

export default App;