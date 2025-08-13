
import React, { useState, useEffect } from 'react';
import { api } from '../lib/api';

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

interface Session {
  id: string;
  device_id: string;
  user_agent?: string;
  ip_last: string;
  created_at: string;
  last_seen_at: string;
}

interface SecurityPageProps {
  user: User;
}

const SecurityPage: React.FC<SecurityPageProps> = ({ user }) => {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [changePasswordForm, setChangePasswordForm] = useState({
    oldPassword: '',
    newPassword: '',
    confirmPassword: ''
  });
  const [showChangePassword, setShowChangePassword] = useState(false);

  useEffect(() => {
    loadSessions();
  }, []);

  const loadSessions = async () => {
    try {
      const response = await api.get('/users/me');
      setSessions(response.data.sessions || []);
    } catch (error) {
      console.error('Failed to load sessions:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (changePasswordForm.newPassword !== changePasswordForm.confirmPassword) {
      alert('New passwords do not match');
      return;
    }

    try {
      await api.post('/me/change-password', {
        old_password: changePasswordForm.oldPassword,
        new_password: changePasswordForm.newPassword
      });
      
      alert('Password changed successfully');
      setShowChangePassword(false);
      setChangePasswordForm({ oldPassword: '', newPassword: '', confirmPassword: '' });
    } catch (error: any) {
      alert(error.response?.data?.detail || 'Failed to change password');
    }
  };

  const revokeSession = async (sessionId: string) => {
    if (!confirm('Are you sure you want to revoke this session?')) return;
    
    try {
      await api.post(`/me/sessions/${sessionId}/revoke`);
      await loadSessions();
      alert('Session revoked successfully');
    } catch (error) {
      alert('Failed to revoke session');
    }
  };

  const revokeAllOtherSessions = async () => {
    if (!confirm('This will sign you out of all other devices. Continue?')) return;
    
    try {
      await api.post('/me/sessions/revoke-others');
      await loadSessions();
      alert('All other sessions revoked successfully');
    } catch (error) {
      alert('Failed to revoke sessions');
    }
  };

  const revokeAllSessions = async () => {
    if (!confirm('This will sign you out of ALL devices including this one. Continue?')) return;
    
    try {
      await api.post('/auth/logout-all');
      window.location.href = '/auth';
    } catch (error) {
      alert('Failed to revoke all sessions');
    }
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString();
  };

  const getDeviceInfo = (userAgent?: string) => {
    if (!userAgent) return 'Unknown device';
    
    if (userAgent.includes('Mobile')) return 'Mobile device';
    if (userAgent.includes('Chrome')) return 'Chrome browser';
    if (userAgent.includes('Firefox')) return 'Firefox browser';
    if (userAgent.includes('Safari')) return 'Safari browser';
    return 'Unknown device';
  };

  return (
    <div className="max-w-4xl mx-auto p-6">
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b border-gray-200">
          <h1 className="text-2xl font-bold text-gray-900">Security & Access</h1>
          <p className="text-gray-600 mt-1">Manage your account security and active sessions</p>
        </div>
        
        <div className="p-6 space-y-8">
          {/* Password Section */}
          <div className="border border-gray-200 rounded-lg p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-lg font-medium text-gray-900">Password</h3>
                <p className="text-gray-600">Update your password to keep your account secure</p>
              </div>
              <button 
                onClick={() => setShowChangePassword(!showChangePassword)}
                className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
              >
                Change Password
              </button>
            </div>
            
            {showChangePassword && (
              <form onSubmit={handleChangePassword} className="mt-4 space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">
                    Current Password
                  </label>
                  <input
                    type="password"
                    value={changePasswordForm.oldPassword}
                    onChange={(e) => setChangePasswordForm({
                      ...changePasswordForm, 
                      oldPassword: e.target.value
                    })}
                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                    required
                  />
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">
                    New Password
                  </label>
                  <input
                    type="password"
                    value={changePasswordForm.newPassword}
                    onChange={(e) => setChangePasswordForm({
                      ...changePasswordForm, 
                      newPassword: e.target.value
                    })}
                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                    minLength={8}
                    required
                  />
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">
                    Confirm New Password
                  </label>
                  <input
                    type="password"
                    value={changePasswordForm.confirmPassword}
                    onChange={(e) => setChangePasswordForm({
                      ...changePasswordForm, 
                      confirmPassword: e.target.value
                    })}
                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                    required
                  />
                </div>
                
                <div className="flex space-x-3">
                  <button
                    type="submit"
                    className="bg-green-600 text-white px-4 py-2 rounded-md hover:bg-green-700"
                  >
                    Update Password
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowChangePassword(false)}
                    className="bg-gray-600 text-white px-4 py-2 rounded-md hover:bg-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            )}
          </div>

          {/* Active Sessions Section */}
          <div className="border border-gray-200 rounded-lg p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-lg font-medium text-gray-900">Active Sessions</h3>
                <p className="text-gray-600">Manage where your account is currently signed in</p>
              </div>
              <div className="space-x-2">
                <button
                  onClick={revokeAllOtherSessions}
                  className="bg-orange-600 text-white px-4 py-2 rounded-md text-sm hover:bg-orange-700"
                >
                  Sign Out Other Devices
                </button>
                <button
                  onClick={revokeAllSessions}
                  className="bg-red-600 text-white px-4 py-2 rounded-md text-sm hover:bg-red-700"
                >
                  Sign Out All Devices
                </button>
              </div>
            </div>
            
            {loading ? (
              <div className="text-center py-4">Loading sessions...</div>
            ) : (
              <div className="space-y-3">
                {sessions.length === 0 ? (
                  <div className="text-gray-500 text-center py-4">No active sessions found</div>
                ) : (
                  sessions.map((session) => (
                    <div key={session.id} className="flex items-center justify-between p-4 border rounded-lg">
                      <div className="flex-1">
                        <div className="flex items-center space-x-2">
                          <span className="font-medium">{getDeviceInfo(session.user_agent)}</span>
                          <span className="text-xs bg-green-100 text-green-800 px-2 py-1 rounded">
                            Current Session
                          </span>
                        </div>
                        <div className="text-sm text-gray-600 mt-1">
                          IP: {session.ip_last} • 
                          Created: {formatDate(session.created_at)} • 
                          Last seen: {formatDate(session.last_seen_at)}
                        </div>
                        <div className="text-xs text-gray-500 mt-1">
                          Device ID: {session.device_id}
                        </div>
                      </div>
                      <button
                        onClick={() => revokeSession(session.id)}
                        className="bg-red-600 text-white px-3 py-1 rounded text-sm hover:bg-red-700"
                      >
                        Revoke
                      </button>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>

          {/* Two-Factor Authentication Placeholder */}
          <div className="border border-gray-200 rounded-lg p-6">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-lg font-medium text-gray-900">Two-Factor Authentication</h3>
                <p className="text-gray-600">Add an extra layer of security to your account</p>
                <p className="text-sm text-yellow-600 mt-1">Coming soon - Multi-factor authentication setup</p>
              </div>
              <button 
                disabled
                className="bg-gray-400 text-white px-4 py-2 rounded-md cursor-not-allowed"
              >
                Enable 2FA
              </button>
            </div>
          </div>

          {/* Account Deletion Warning */}
          <div className="border border-red-200 rounded-lg p-6 bg-red-50">
            <h3 className="text-lg font-medium text-red-900 mb-2">Danger Zone</h3>
            <p className="text-red-700 mb-4">
              These actions are irreversible. Please be certain before proceeding.
            </p>
            <button className="bg-red-600 text-white px-4 py-2 rounded-md hover:bg-red-700">
              Delete Account
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SecurityPage;
