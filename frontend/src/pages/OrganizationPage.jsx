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