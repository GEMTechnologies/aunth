
import React, { useState, FC } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

type FormState = 'login' | 'register' | 'forgotPassword';

const LoginPage: FC = () => {
  const [currentForm, setCurrentForm] = useState<FormState>('register'); // Default to register for now

  const formVariants = {
    initial: { opacity: 0, y: 20 },
    animate: { opacity: 1, y: 0 },
    exit: { opacity: 0, y: -20 },
  };

  const renderForm = () => {
    switch (currentForm) {
      case 'login':
        return (
          <motion.form
            key="loginForm"
            variants={formVariants}
            initial="initial"
            animate="animate"
            exit="exit"
            transition={{ duration: 0.3, ease: "easeOut" }}
            className="w-full flex flex-col gap-4 mb-6"
          >
            <h2 className="text-2xl font-semibold mb-4 text-gray-800">Welcome Back</h2>
            <input type="email" placeholder="Email Address" className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all duration-300 ease-in-out" />
            <input type="password" placeholder="Password" className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all duration-300 ease-in-out" />
            <button type="submit" className="w-full bg-blue-600 text-white py-3 rounded-lg text-lg font-semibold hover:bg-blue-700 transition-all duration-300 ease-in-out transform hover:-translate-y-1 active:translate-y-0">Sign In</button>
          </motion.form>
        );
      case 'register':
        return (
          <motion.form
            key="registerForm"
            variants={formVariants}
            initial="initial"
            animate="animate"
            exit="exit"
            transition={{ duration: 0.3, ease: "easeOut" }}
            className="w-full flex flex-col gap-4 mb-6"
          >
            <h2 className="text-2xl font-semibold mb-4 text-gray-800">Create Your Account</h2>
            <input type="text" placeholder="Your Full Name" className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all duration-300 ease-in-out" />
            <input type="text" placeholder="Organization Name" className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all duration-300 ease-in-out" />
            <input type="email" placeholder="Work Email Address" className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all duration-300 ease-in-out" />
            <input type="password" placeholder="Create Password" className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all duration-300 ease-in-out" />
            <div className="flex items-center gap-2 text-sm text-gray-600 text-left">
              <input type="checkbox" id="terms" className="accent-blue-600 w-4 h-4" />
              <label htmlFor="terms">
                I agree to the <a href="#" className="text-blue-600 font-semibold hover:underline">Terms of Service</a> and <a href="#" className="text-blue-600 font-semibold hover:underline">Privacy Policy</a>
              </label>
            </div>
            <button type="submit" className="w-full bg-blue-600 text-white py-3 rounded-lg text-lg font-semibold hover:bg-blue-700 transition-all duration-300 ease-in-out transform hover:-translate-y-1 active:translate-y-0">Create Account</button>
          </motion.form>
        );
      case 'forgotPassword':
        return (
          <motion.form
            key="forgotPasswordForm"
            variants={formVariants}
            initial="initial"
            animate="animate"
            exit="exit"
            transition={{ duration: 0.3, ease: "easeOut" }}
            className="w-full flex flex-col gap-4 mb-6"
          >
            <h2 className="text-2xl font-semibold mb-4 text-gray-800">Reset Your Password</h2>
            <p className="text-gray-600 text-sm mb-4">Enter your email and we'll send you a link to reset your password.</p>
            <input type="email" placeholder="Email Address" className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all duration-300 ease-in-out" />
            <button type="submit" className="w-full bg-blue-600 text-white py-3 rounded-lg text-lg font-semibold hover:bg-blue-700 transition-all duration-300 ease-in-out transform hover:-translate-y-1 active:translate-y-0">Send Reset Link</button>
          </motion.form>
        );
      default:
        return null;
    }
  };

  return (
    <div className="flex flex-col md:flex-row min-h-screen font-sans text-gray-800">
      {/* Left Column - Branding & Inspiration */}
      <div className="flex-1 bg-gradient-to-br from-purple-600 to-blue-500 flex items-center justify-center p-10 relative overflow-hidden">
        <div className="text-center text-white z-10">
          {/* Placeholder for inspiring image or animation */}
          <p className="text-4xl md:text-5xl font-extrabold leading-tight mb-4 tracking-tight">"The Operating System for Impact."</p>
          <p className="text-lg md:text-xl font-medium">Plan. Fund. Execute. Measure.</p>
        </div>
        {/* Abstract background elements (optional) */}
        <div className="absolute inset-0 z-0 opacity-20">
            {/* Example: SVG pattern or subtle animation */}
        </div>
      </div>

      {/* Right Column - Functional Area */}
      <div className="flex-1 flex items-center justify-center bg-gray-50 p-6 md:p-10">
        <div className="max-w-md w-full bg-white p-8 md:p-10 rounded-xl shadow-lg text-center">
          {/* Granada Logo Placeholder */}
          <img src="/path/to/granada-logo.svg" alt="Granada Logo" className="max-w-[120px] mx-auto mb-8" />

          <AnimatePresence mode="wait">
            {renderForm()}
          </AnimatePresence>

          {/* Social Sign-On */}
          <div className="mt-6 pt-6 border-t border-gray-200 flex flex-col gap-3">
            <button className="flex items-center justify-center w-full px-4 py-3 border border-gray-300 rounded-lg bg-white text-gray-700 text-base font-medium hover:bg-gray-100 transition-colors duration-200 ease-in-out">
              {/* Google Icon Placeholder */}
              <img src="/path/to/google-icon.svg" alt="Google" className="w-5 h-5 mr-3" />
              Continue with Google
            </button>
            <button className="flex items-center justify-center w-full px-4 py-3 border border-gray-300 rounded-lg bg-white text-gray-700 text-base font-medium hover:bg-gray-100 transition-colors duration-200 ease-in-out">
              {/* GitHub Icon Placeholder */}
              <img src="/path/to/github-icon.svg" alt="GitHub" className="w-5 h-5 mr-3" />
              Continue with GitHub
            </button>
          </div>

          {/* State-Switching Links */}
          <div className="mt-6 text-sm text-gray-600">
            {currentForm === 'login' && (
              <>
                <p className="mb-2">Don't have an account? <span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setCurrentForm('register')}>Sign Up</span></p>
                <p><span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setCurrentForm('forgotPassword')}>Forgot Password?</span></p>
              </>
            )}
            {currentForm === 'register' && (
              <p>Already have an account? <span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setCurrentForm('login')}>Sign In</span></p>
            )}
            {currentForm === 'forgotPassword' && (
                <p>Remember your password? <span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setCurrentForm('login')}>Sign In</span></p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default LoginPage;

