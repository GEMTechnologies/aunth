import React from 'react';
import ProfileForm from '../components/ProfileForm';

const ProfilePage = () => {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-purple-600 to-blue-500 font-sans text-gray-800">
      <div className="max-w-lg w-full bg-white/80 backdrop-blur-lg p-10 rounded-3xl shadow-2xl text-center border border-white/40 mx-4 transition-all duration-300">
        <img src="https://via.placeholder.com/120x50.png?text=Granada" alt="Granada Logo" className="max-w-[120px] mx-auto mb-8 drop-shadow-lg" />
        <h1 className="text-3xl font-extrabold text-gray-900 mb-2 tracking-tight">My Profile</h1>
        <p className="text-gray-600 mb-8">Update your personal information and preferences.</p>
        <div className="bg-white/90 rounded-2xl shadow p-6 mb-6 border border-gray-100">
          <ProfileForm />
        </div>
        <div className="mt-6 text-xs text-gray-400">Granada Platform &copy; {new Date().getFullYear()}</div>
      </div>
    </div>
  );
};

export default ProfilePage;
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

interface ProfilePageProps {
  user: User;
  onUserUpdate: (user: User) => void;
}

const ProfilePage: React.FC<ProfilePageProps> = ({ user, onUserUpdate }) => {
  return (
    <div className="max-w-4xl mx-auto p-6">
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b border-gray-200">
          <h1 className="text-2xl font-bold text-gray-900">Profile</h1>
        </div>
        <div className="p-6">
          <div className="space-y-6">
            <div className="flex items-center space-x-4">
              <div className="w-16 h-16 bg-blue-500 rounded-full flex items-center justify-center text-white text-xl font-bold">
                {user.display_name?.charAt(0) || user.primary_email?.email.charAt(0).toUpperCase()}
              </div>
              <div>
                <h2 className="text-xl font-semibold text-gray-900">{user.display_name}</h2>
                <p className="text-gray-600">{user.primary_email?.email}</p>
              </div>
            </div>
            
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Display Name</label>
                <input
                  type="text"
                  value={user.display_name}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                  readOnly
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Email</label>
                <input
                  type="email"
                  value={user.primary_email?.email || ''}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                  readOnly
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ProfilePage;
