import React, { useState, useEffect } from 'react';
import AuthPage from './pages/AuthPage';
import ProfilePage from './pages/ProfilePage';
import OrganizationPage from './pages/OrganizationPage';
import SecurityPage from './pages/SecurityPage';
import { apiMe, apiLogout, restoreSession } from './lib/api';
import './index.css';

// Assume these components are defined elsewhere and handle context selection and routing
import ContextRouter from './components/ContextRouter'; // Resolves which context a user lands in

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

  /**
   * On boot there is no token in memory: credentials are not persisted to
   * localStorage. The session is therefore re-established from the HttpOnly
   * refresh cookie, which the browser sends without the page having to hold it.
   * A visitor with no cookie simply lands on the auth page, which is not an
   * error worth logging.
   */
  const checkAuth = async () => {
    try {
      const restored = await restoreSession();
      if (!restored) {
        setLoading(false);
        return;
      }

      const userData = await apiMe();
      setUser(userData);
      setCurrentPage('profile');
    } catch (error) {
      console.error('Auth check failed:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleLogin = (userData: User) => {
    setUser(userData);
    setShowContextRouter(true);
  };

  const handleLogout = async () => {
    await apiLogout();
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

  // Gate the app on context resolution. This is ContextRouter, not
  // ContextPicker: ContextRouter resolves which context the user should land in
  // from the account itself, which is what a returning user needs. ContextPicker
  // is an interactive list and has never been wired up here.
  if (showContextRouter && user) {
    return <ContextRouter user={user} onContextResolved={handleContextResolved} />;
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
        {/* The resolved context is recorded on state; it is not a reason to
            re-enter context resolution, which is what this used to do. Once
            `handleContextResolved` had fired, every page under this nav was
            replaced by ContextRouter, so the application appeared to vanish
            the moment it finished signing in. */}
        <>
          {currentPage === 'profile' && <ProfilePage user={user} onUserUpdate={setUser} />}
          {currentPage === 'organizations' && <OrganizationPage user={user} />}
          {currentPage === 'security' && <SecurityPage user={user} />}
        </>
      </main>
    </div>
  );
}

export default App;