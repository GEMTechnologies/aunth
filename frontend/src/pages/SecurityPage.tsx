
import React from 'react';

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

interface SecurityPageProps {
  user: User;
}

const SecurityPage: React.FC<SecurityPageProps> = ({ user }) => {
  return (
    <div className="max-w-4xl mx-auto p-6">
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b border-gray-200">
          <h1 className="text-2xl font-bold text-gray-900">Security</h1>
        </div>
        <div className="p-6 space-y-6">
          <div className="border border-gray-200 rounded-lg p-4">
            <h3 className="text-lg font-medium text-gray-900 mb-2">Password</h3>
            <p className="text-gray-600 mb-4">Update your password to keep your account secure.</p>
            <button className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700">
              Change Password
            </button>
          </div>
          
          <div className="border border-gray-200 rounded-lg p-4">
            <h3 className="text-lg font-medium text-gray-900 mb-2">Two-Factor Authentication</h3>
            <p className="text-gray-600 mb-4">Add an extra layer of security to your account.</p>
            <button className="bg-green-600 text-white px-4 py-2 rounded-md hover:bg-green-700">
              Enable 2FA
            </button>
          </div>
          
          <div className="border border-gray-200 rounded-lg p-4">
            <h3 className="text-lg font-medium text-gray-900 mb-2">Active Sessions</h3>
            <p className="text-gray-600 mb-4">Manage your active sessions across devices.</p>
            <div className="space-y-2">
              <div className="flex justify-between items-center p-2 bg-gray-50 rounded">
                <span className="text-sm">Current Session</span>
                <span className="text-xs text-gray-500">Active now</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SecurityPage;
