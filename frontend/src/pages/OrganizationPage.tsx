
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

interface Organization {
  id: string;
  name: string;
  slug: string;
  created_at: string;
}

interface OrgMember {
  user_id: string;
  role: string;
  joined_at: string;
  user: {
    id: string;
    display_name: string;
    primary_email?: {
      email: string;
      is_verified: boolean;
    };
  };
}

interface OrganizationPageProps {
  user: User;
}

const OrganizationPage: React.FC<OrganizationPageProps> = ({ user }) => {
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [selectedOrg, setSelectedOrg] = useState<Organization | null>(null);
  const [members, setMembers] = useState<OrgMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'overview' | 'members' | 'settings'>('overview');
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('member');
  const [newOrgName, setNewOrgName] = useState('');
  const [showCreateOrg, setShowCreateOrg] = useState(false);

  useEffect(() => {
    loadOrganizations();
  }, []);

  useEffect(() => {
    if (selectedOrg) {
      loadMembers();
    }
  }, [selectedOrg]);

  const loadOrganizations = async () => {
    try {
      const response = await api.get('/organizations');
      setOrganizations(response.data);
      if (response.data.length > 0 && !selectedOrg) {
        setSelectedOrg(response.data[0]);
      }
    } catch (error) {
      console.error('Failed to load organizations:', error);
    } finally {
      setLoading(false);
    }
  };

  const loadMembers = async () => {
    if (!selectedOrg) return;
    
    try {
      const response = await api.get(`/orgs/${selectedOrg.id}/members`);
      setMembers(response.data);
    } catch (error) {
      console.error('Failed to load members:', error);
    }
  };

  const createOrganization = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newOrgName.trim()) return;

    try {
      const response = await api.post('/organizations', { name: newOrgName });
      await loadOrganizations();
      setSelectedOrg(response.data);
      setNewOrgName('');
      setShowCreateOrg(false);
      alert('Organization created successfully');
    } catch (error: any) {
      alert(error.response?.data?.detail || 'Failed to create organization');
    }
  };

  const inviteMember = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedOrg || !inviteEmail.trim()) return;

    try {
      await api.post(`/orgs/${selectedOrg.id}/invites`, {
        email: inviteEmail,
        role: inviteRole
      });
      
      setInviteEmail('');
      alert('Invitation sent successfully');
      await loadMembers();
    } catch (error: any) {
      alert(error.response?.data?.detail || 'Failed to send invitation');
    }
  };

  const changeMemberRole = async (memberId: string, newRole: string) => {
    if (!selectedOrg) return;
    
    try {
      await api.patch(`/orgs/${selectedOrg.id}/members/${memberId}`, {
        role: newRole
      });
      
      await loadMembers();
      alert('Member role updated successfully');
    } catch (error: any) {
      alert(error.response?.data?.detail || 'Failed to update member role');
    }
  };

  const removeMember = async (memberId: string) => {
    if (!selectedOrg) return;
    if (!confirm('Are you sure you want to remove this member?')) return;
    
    try {
      await api.delete(`/orgs/${selectedOrg.id}/members/${memberId}`);
      await loadMembers();
      alert('Member removed successfully');
    } catch (error: any) {
      alert(error.response?.data?.detail || 'Failed to remove member');
    }
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString();
  };

  const getRoleBadgeColor = (role: string) => {
    switch (role) {
      case 'admin': return 'bg-red-100 text-red-800';
      case 'moderator': return 'bg-yellow-100 text-yellow-800';
      case 'member': return 'bg-blue-100 text-blue-800';
      default: return 'bg-gray-100 text-gray-800';
    }
  };

  if (loading) {
    return (
      <div className="max-w-6xl mx-auto p-6">
        <div className="text-center py-8">Loading organizations...</div>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto p-6">
      <div className="bg-white shadow rounded-lg">
        {/* Header */}
        <div className="px-6 py-4 border-b border-gray-200">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-gray-900">Organizations</h1>
              <p className="text-gray-600 mt-1">Manage your organization workspaces</p>
            </div>
            <button
              onClick={() => setShowCreateOrg(true)}
              className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
            >
              Create Organization
            </button>
          </div>
        </div>

        {/* Organization Selector */}
        {organizations.length > 0 && (
          <div className="px-6 py-4 border-b border-gray-200 bg-gray-50">
            <div className="flex items-center space-x-4">
              <label className="text-sm font-medium text-gray-700">Organization:</label>
              <select
                value={selectedOrg?.id || ''}
                onChange={(e) => {
                  const org = organizations.find(o => o.id === e.target.value);
                  setSelectedOrg(org || null);
                }}
                className="px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {organizations.map(org => (
                  <option key={org.id} value={org.id}>{org.name}</option>
                ))}
              </select>
            </div>
          </div>
        )}

        {/* Create Organization Modal */}
        {showCreateOrg && (
          <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div className="bg-white rounded-lg p-6 w-full max-w-md">
              <h2 className="text-lg font-bold mb-4">Create New Organization</h2>
              <form onSubmit={createOrganization}>
                <div className="mb-4">
                  <label className="block text-sm font-medium text-gray-700 mb-2">
                    Organization Name
                  </label>
                  <input
                    type="text"
                    value={newOrgName}
                    onChange={(e) => setNewOrgName(e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                    placeholder="Enter organization name"
                    required
                  />
                </div>
                <div className="flex space-x-3">
                  <button
                    type="submit"
                    className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
                  >
                    Create
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowCreateOrg(false)}
                    className="bg-gray-600 text-white px-4 py-2 rounded-md hover:bg-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}

        {organizations.length === 0 ? (
          <div className="p-6 text-center">
            <div className="text-gray-500 mb-4">You are not a member of any organizations yet.</div>
            <button
              onClick={() => setShowCreateOrg(true)}
              className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
            >
              Create Your First Organization
            </button>
          </div>
        ) : selectedOrg && (
          <>
            {/* Tabs */}
            <div className="px-6 border-b border-gray-200">
              <nav className="flex space-x-8">
                <button
                  onClick={() => setActiveTab('overview')}
                  className={`py-4 px-1 border-b-2 font-medium text-sm ${
                    activeTab === 'overview'
                      ? 'border-blue-500 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  Overview
                </button>
                <button
                  onClick={() => setActiveTab('members')}
                  className={`py-4 px-1 border-b-2 font-medium text-sm ${
                    activeTab === 'members'
                      ? 'border-blue-500 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  Members
                </button>
                <button
                  onClick={() => setActiveTab('settings')}
                  className={`py-4 px-1 border-b-2 font-medium text-sm ${
                    activeTab === 'settings'
                      ? 'border-blue-500 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  Settings
                </button>
              </nav>
            </div>

            {/* Tab Content */}
            <div className="p-6">
              {activeTab === 'overview' && (
                <div className="space-y-6">
                  <div>
                    <h3 className="text-lg font-medium text-gray-900 mb-2">Organization Details</h3>
                    <div className="bg-gray-50 rounded-lg p-4">
                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <label className="block text-sm font-medium text-gray-700">Name</label>
                          <p className="text-gray-900">{selectedOrg.name}</p>
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-gray-700">Slug</label>
                          <p className="text-gray-900">{selectedOrg.slug}</p>
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-gray-700">Created</label>
                          <p className="text-gray-900">{formatDate(selectedOrg.created_at)}</p>
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-gray-700">Members</label>
                          <p className="text-gray-900">{members.length}</p>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'members' && (
                <div className="space-y-6">
                  {/* Invite Member Form */}
                  <div className="border border-gray-200 rounded-lg p-4">
                    <h3 className="text-lg font-medium text-gray-900 mb-4">Invite New Member</h3>
                    <form onSubmit={inviteMember} className="flex space-x-4">
                      <input
                        type="email"
                        value={inviteEmail}
                        onChange={(e) => setInviteEmail(e.target.value)}
                        placeholder="Enter email address"
                        className="flex-1 px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                        required
                      />
                      <select
                        value={inviteRole}
                        onChange={(e) => setInviteRole(e.target.value)}
                        className="px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                      >
                        <option value="member">Member</option>
                        <option value="moderator">Moderator</option>
                        <option value="admin">Admin</option>
                      </select>
                      <button
                        type="submit"
                        className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
                      >
                        Send Invite
                      </button>
                    </form>
                  </div>

                  {/* Members List */}
                  <div>
                    <h3 className="text-lg font-medium text-gray-900 mb-4">Current Members</h3>
                    <div className="space-y-3">
                      {members.map((member) => (
                        <div key={member.user_id} className="flex items-center justify-between p-4 border rounded-lg">
                          <div className="flex items-center space-x-4">
                            <div className="w-10 h-10 bg-gray-300 rounded-full flex items-center justify-center">
                              <span className="text-gray-600 font-medium">
                                {member.user.display_name?.[0]?.toUpperCase() || '?'}
                              </span>
                            </div>
                            <div>
                              <div className="font-medium text-gray-900">
                                {member.user.display_name || 'Unknown User'}
                              </div>
                              <div className="text-sm text-gray-600">
                                {member.user.primary_email?.email}
                              </div>
                            </div>
                            <span className={`px-2 py-1 text-xs font-medium rounded-full ${getRoleBadgeColor(member.role)}`}>
                              {member.role}
                            </span>
                          </div>
                          <div className="flex items-center space-x-2">
                            <select
                              value={member.role}
                              onChange={(e) => changeMemberRole(member.user_id, e.target.value)}
                              className="text-sm px-2 py-1 border border-gray-300 rounded"
                            >
                              <option value="member">Member</option>
                              <option value="moderator">Moderator</option>
                              <option value="admin">Admin</option>
                            </select>
                            <button
                              onClick={() => removeMember(member.user_id)}
                              className="bg-red-600 text-white px-3 py-1 rounded text-sm hover:bg-red-700"
                            >
                              Remove
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'settings' && (
                <div className="space-y-6">
                  <div className="border border-gray-200 rounded-lg p-4">
                    <h3 className="text-lg font-medium text-gray-900 mb-2">Organization Settings</h3>
                    <p className="text-gray-600 mb-4">Advanced organization configuration options</p>
                    <p className="text-sm text-yellow-600">
                      Settings panel coming soon - organization branding, permissions, and more
                    </p>
                  </div>
                  
                  <div className="border border-red-200 rounded-lg p-4 bg-red-50">
                    <h3 className="text-lg font-medium text-red-900 mb-2">Danger Zone</h3>
                    <p className="text-red-700 mb-4">
                      These actions are irreversible and will affect all organization members.
                    </p>
                    <button className="bg-red-600 text-white px-4 py-2 rounded-md hover:bg-red-700">
                      Delete Organization
                    </button>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
};

export default OrganizationPage;
