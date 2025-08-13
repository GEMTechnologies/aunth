
import React, { useEffect, useState } from 'react';
import { apiResolveContext, apiSetLastContext } from '../lib/api';
import ContextPicker from './ContextPicker';

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

interface Context {
  type: string;
  product?: string;
  role?: string;
  org_id?: string;
  org_slug?: string;
  org_name?: string;
}

interface ContextRouterProps {
  user: User;
  onContextResolved: (context: Context) => void;
}

const ContextRouter: React.FC<ContextRouterProps> = ({ user, onContextResolved }) => {
  const [resolution, setResolution] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    resolveUserContext();
  }, []);

  const resolveUserContext = async () => {
    try {
      setLoading(true);
      const urlParams = new URLSearchParams(window.location.search);
      const redirectUri = urlParams.get('redirect_uri');
      
      const result = await apiResolveContext(redirectUri || undefined);
      setResolution(result);

      if (result.action === 'redirect') {
        // Automatically redirect to resolved context
        onContextResolved(result.context);
        window.location.href = result.url;
      }
    } catch (err: any) {
      setError(err.message || 'Failed to resolve context');
    } finally {
      setLoading(false);
    }
  };

  const handleContextSelect = async (context: Context, makeDefault: boolean) => {
    try {
      if (makeDefault) {
        await apiSetLastContext(context);
      }
      onContextResolved(context);
      
      // For now, navigate to a default dashboard
      // In a real app, you'd use proper routing
      const url = getUrlForContext(context);
      window.location.href = url;
    } catch (err: any) {
      setError(err.message || 'Failed to set context');
    }
  };

  const getUrlForContext = (context: Context): string => {
    if (context.type === 'student') {
      return '/student/dashboard';
    } else if (context.type === 'org') {
      const product = context.product || 'ngos';
      const orgSlug = context.org_slug || context.org_id;
      return `/${product}/${orgSlug}/dashboard`;
    }
    return '/dashboard';
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-32 w-32 border-b-2 border-blue-600 mx-auto"></div>
          <p className="mt-4 text-gray-600">Resolving your workspace...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-600 text-xl mb-4">⚠️</div>
          <h2 className="text-xl font-semibold text-gray-900 mb-2">Something went wrong</h2>
          <p className="text-gray-600 mb-4">{error}</p>
          <button
            onClick={resolveUserContext}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
          >
            Try Again
          </button>
        </div>
      </div>
    );
  }

  if (resolution?.action === 'show_picker' || resolution?.action === 'show_product_picker') {
    const contexts = resolution.contexts || [];
    const title = resolution.action === 'show_product_picker' 
      ? `Choose ${resolution.product} Workspace` 
      : 'Choose Your Workspace';

    return (
      <ContextPicker
        contexts={contexts}
        onContextSelect={handleContextSelect}
        title={title}
      />
    );
  }

  // Fallback
  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="text-center">
        <h2 className="text-xl font-semibold text-gray-900 mb-2">Welcome!</h2>
        <p className="text-gray-600">Setting up your workspace...</p>
      </div>
    </div>
  );
};

export default ContextRouter;
