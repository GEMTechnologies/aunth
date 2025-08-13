
import React, { useState } from 'react';
import { motion } from 'framer-motion';

interface Context {
  type: string;
  product?: string;
  role?: string;
  org_id?: string;
  org_slug?: string;
  org_name?: string;
}

interface ContextPickerProps {
  contexts: Context[];
  onContextSelect: (context: Context, makeDefault: boolean) => void;
  title?: string;
}

const ContextPicker: React.FC<ContextPickerProps> = ({ 
  contexts, 
  onContextSelect, 
  title = "Choose Your Workspace" 
}) => {
  const [makeDefault, setMakeDefault] = useState(false);

  const getContextDisplayName = (context: Context) => {
    if (context.type === 'student') {
      return 'Student (Academia)';
    } else if (context.type === 'org') {
      const productName = {
        'ngos': 'NGOs',
        'business': 'Business',
        'jobs': 'Jobs'
      }[context.product || 'ngos'] || 'Organization';
      
      return `${context.org_name || context.org_slug} (${productName})`;
    }
    return 'Unknown Context';
  };

  const getContextIcon = (context: Context) => {
    if (context.type === 'student') {
      return (
        <div className="w-12 h-12 bg-blue-500 rounded-lg flex items-center justify-center">
          <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.746 0 3.332.477 4.5 1.253v13C19.832 18.477 18.246 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
          </svg>
        </div>
      );
    } else {
      return (
        <div className="w-12 h-12 bg-green-500 rounded-lg flex items-center justify-center">
          <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
          </svg>
        </div>
      );
    }
  };

  const getRoleDisplayName = (context: Context) => {
    const roleMap = {
      'student': 'Student',
      'ngo_owner': 'Owner',
      'ngo_staff': 'Staff Member',
      'business_owner': 'Owner',
      'business_staff': 'Staff Member'
    };
    return roleMap[context.role as keyof typeof roleMap] || context.role || '';
  };

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-white rounded-lg shadow-lg max-w-md w-full p-6"
      >
        <div className="text-center mb-6">
          <h1 className="text-2xl font-bold text-gray-900 mb-2">{title}</h1>
          <p className="text-gray-600">Select the workspace you'd like to access</p>
        </div>

        <div className="space-y-3">
          {contexts.map((context, index) => (
            <motion.button
              key={index}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: index * 0.1 }}
              onClick={() => onContextSelect(context, makeDefault)}
              className="w-full flex items-center p-4 border border-gray-200 rounded-lg hover:border-blue-300 hover:bg-blue-50 transition-colors text-left"
            >
              {getContextIcon(context)}
              <div className="ml-4 flex-1">
                <h3 className="font-semibold text-gray-900">
                  {getContextDisplayName(context)}
                </h3>
                {context.role && (
                  <p className="text-sm text-gray-600">
                    {getRoleDisplayName(context)}
                  </p>
                )}
              </div>
              <svg className="w-5 h-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
            </motion.button>
          ))}
        </div>

        <div className="mt-6 pt-6 border-t border-gray-200">
          <label className="flex items-center">
            <input
              type="checkbox"
              checked={makeDefault}
              onChange={(e) => setMakeDefault(e.target.checked)}
              className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            <span className="ml-2 text-sm text-gray-600">
              Make this my default workspace
            </span>
          </label>
        </div>
      </motion.div>
    </div>
  );
};

export default ContextPicker;
