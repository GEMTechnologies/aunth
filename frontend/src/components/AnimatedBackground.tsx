
import React from 'react';
import { motion } from 'framer-motion';

const AnimatedBackground: React.FC = () => {
  return (
    <div className="flex-1 relative overflow-hidden flex items-center justify-center p-10 bg-gradient-to-br from-blue-700 via-purple-600 to-fuchsia-500 animate-gradient-x">
      <motion.div
        className="absolute inset-0 z-0 pointer-events-none"
        initial={{ opacity: 0 }}
        animate={{ opacity: 0.25 }}
        transition={{ duration: 2 }}
      >
        <div className="absolute top-1/4 left-1/4 w-60 h-60 bg-white/10 rounded-full blur-2xl animate-pulse" />
        <div className="absolute bottom-10 right-10 w-40 h-40 bg-white/20 rounded-full blur-2xl animate-pulse" />
      </motion.div>
      <div className="text-center text-white z-10 drop-shadow-xl">
        <motion.p
          className="text-5xl md:text-6xl font-extrabold leading-tight mb-4 tracking-tight"
          initial={{ opacity: 0, y: 40 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.8, type: 'spring' }}
        >
          "The Operating System for Impact."
        </motion.p>
        <motion.p
          className="text-2xl md:text-3xl font-medium"
          initial={{ opacity: 0, y: 40 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 1, delay: 0.2, type: 'spring' }}
        >
          Plan. Fund. Execute. Measure.
        </motion.p>
      </div>
    </div>
  );
};

export default AnimatedBackground;
