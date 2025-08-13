
import React from 'react';
import { motion } from 'framer-motion';

interface SocialButtonProps {
  provider: 'google' | 'github';
  icon: string;
}

const SocialButton: React.FC<SocialButtonProps> = ({ provider, icon }) => {
  return (
    <motion.button
      whileTap={{ scale: 0.97 }}
      whileHover={{ scale: 1.03 }}
      className="flex items-center justify-center w-full px-4 py-3 border border-gray-300 rounded-2xl bg-white text-gray-700 text-base font-semibold shadow hover:bg-gray-100 transition-colors duration-200"
    >
      <img src={icon} alt={provider} className="w-5 h-5 mr-3" />
      Continue with {provider.charAt(0).toUpperCase() + provider.slice(1)}
    </motion.button>
  );
};

export default SocialButton;
