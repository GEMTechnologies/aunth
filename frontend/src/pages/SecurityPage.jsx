import React from 'react';

const SecurityPage = () => {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-purple-600 to-blue-500 font-sans text-gray-800">
      <div className="max-w-lg w-full bg-white/80 backdrop-blur-lg p-10 rounded-3xl shadow-2xl text-center border border-white/40 mx-4 transition-all duration-300">
        <img src="https://via.placeholder.com/120x50.png?text=Granada" alt="Granada Logo" className="max-w-[120px] mx-auto mb-8 drop-shadow-lg" />
        <h1 className="text-3xl font-extrabold text-gray-900 mb-2 tracking-tight">Security</h1>
        <p className="text-gray-600 mb-8">Manage your account security and change your password.</p>
        <div className="bg-white/90 rounded-2xl shadow p-6 mb-6 border border-gray-100">
          <h3 className="text-lg font-semibold text-gray-900 mb-2">Change Password</h3>
          <p className="text-sm text-gray-500 mb-4">Update your password to keep your account secure.</p>
          <form className="mt-2 flex flex-col gap-4 items-center">
            <input
              type="password"
              name="password"
              id="password"
              className="shadow-sm focus:ring-indigo-500 focus:border-indigo-500 block w-full sm:text-sm border-gray-300 rounded-md px-4 py-3 transition-all duration-200"
              placeholder="New Password"
            />
            <button
              type="submit"
              className="w-full inline-flex items-center justify-center px-4 py-3 border border-transparent shadow-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 text-base transition-all duration-200"
            >
              Save
            </button>
          </form>
        </div>
        <div className="mt-6 text-xs text-gray-400">Granada Platform &copy; {new Date().getFullYear()}</div>
      </div>
    </div>
  );
};

export default SecurityPage;