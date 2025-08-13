import React from 'react';
import InviteUserModal from '../components/InviteUserModal';

const OrganizationPage = () => {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-purple-600 to-blue-500 font-sans text-gray-800">
      <div className="max-w-lg w-full bg-white/80 backdrop-blur-lg p-10 rounded-3xl shadow-2xl text-center border border-white/40 mx-4 transition-all duration-300">
        <img src="https://via.placeholder.com/120x50.png?text=Granada" alt="Granada Logo" className="max-w-[120px] mx-auto mb-8 drop-shadow-lg" />
        <h1 className="text-3xl font-extrabold text-gray-900 mb-2 tracking-tight">Organization</h1>
        <p className="text-gray-600 mb-8">Manage your organization and invite new users.</p>
        <div className="bg-white/90 rounded-2xl shadow p-6 mb-6 border border-gray-100">
          <h3 className="text-lg font-semibold text-gray-900 mb-2">Users</h3>
          <p className="text-sm text-gray-500 mb-4">Invite and manage users in your organization.</p>
          <InviteUserModal />
        </div>
        <div className="mt-6 text-xs text-gray-400">Granada Platform &copy; {new Date().getFullYear()}</div>
      </div>
    </div>
  );
};

export default OrganizationPage;
import React from 'react';

const OrganizationPage: React.FC = () => {
  return (
    <div className="max-w-4xl mx-auto p-6">
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b border-gray-200">
          <h1 className="text-2xl font-bold text-gray-900">Organizations</h1>
        </div>
        <div className="p-6">
          <div className="text-center py-12">
            <div className="w-16 h-16 bg-gray-200 rounded-full mx-auto mb-4 flex items-center justify-center">
              <svg className="w-8 h-8 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
              </svg>
            </div>
            <h3 className="text-lg font-medium text-gray-900 mb-2">No organizations yet</h3>
            <p className="text-gray-600 mb-4">You haven't joined or created any organizations.</p>
            <button className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700">
              Create Organization
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default OrganizationPage;
