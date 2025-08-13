
import React from 'react';
import { motion } from 'framer-motion';
import { AuthFormState } from '../pages/AuthPage';

interface AuthFormProps {
  form: AuthFormState;
  setForm: (form: AuthFormState) => void;
}

const formVariants = {
  initial: { opacity: 0, scale: 0.95, y: 40 },
  animate: { opacity: 1, scale: 1, y: 0 },
  exit: { opacity: 0, scale: 0.95, y: -40 },
};

const AuthForm: React.FC<AuthFormProps> = ({ form, setForm }) => {
  const renderForm = () => {
    switch (form) {
      case 'login':
        return (
          <motion.form
            key="login"
            variants={formVariants}
            initial="initial"
            animate="animate"
            exit="exit"
            transition={{ duration: 0.5, type: 'spring', bounce: 0.25 }}
            className="w-full flex flex-col gap-5 mb-8"
          >
            <h2 className="text-3xl font-extrabold mb-2 text-gray-800 tracking-tight">Welcome Back</h2>
            <input type="email" placeholder="Email Address" className="w-full px-5 py-3 rounded-2xl border border-gray-200 bg-white/80 shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-300 text-lg" />
            <input type="password" placeholder="Password" className="w-full px-5 py-3 rounded-2xl border border-gray-200 bg-white/80 shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-300 text-lg" />
            <button type="submit" className="w-full bg-gradient-to-r from-blue-600 via-purple-500 to-fuchsia-500 text-white py-3 rounded-2xl text-lg font-bold shadow-lg hover:scale-105 hover:shadow-xl transition-all duration-300">Sign In</button>
          </motion.form>
        );
      case 'register':
        return (
          <motion.form
            key="register"
            variants={formVariants}
            initial="initial"
            animate="animate"
            exit="exit"
            transition={{ duration: 0.5, type: 'spring', bounce: 0.25 }}
            className="w-full flex flex-col gap-5 mb-8"
          >
            <h2 className="text-3xl font-extrabold mb-2 text-gray-800 tracking-tight">Create Account</h2>
            <input type="text" placeholder="Full Name" className="w-full px-5 py-3 rounded-2xl border border-gray-200 bg-white/80 shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-300 text-lg" />
            <input type="text" placeholder="Organization Name" className="w-full px-5 py-3 rounded-2xl border border-gray-200 bg-white/80 shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-300 text-lg" />
            <input type="email" placeholder="Work Email Address" className="w-full px-5 py-3 rounded-2xl border border-gray-200 bg-white/80 shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-300 text-lg" />
            <input type="password" placeholder="Create Password" className="w-full px-5 py-3 rounded-2xl border border-gray-200 bg-white/80 shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-300 text-lg" />
            <div className="flex items-center gap-2 text-sm text-gray-600 text-left">
              <input type="checkbox" id="terms" className="accent-blue-600 w-4 h-4" />
              <label htmlFor="terms">
                I agree to the <a href="#" className="text-blue-600 font-semibold hover:underline">Terms of Service</a> and <a href="#" className="text-blue-600 font-semibold hover:underline">Privacy Policy</a>
              </label>
            </div>
            <button type="submit" className="w-full bg-gradient-to-r from-blue-600 via-purple-500 to-fuchsia-500 text-white py-3 rounded-2xl text-lg font-bold shadow-lg hover:scale-105 hover:shadow-xl transition-all duration-300">Create Account</button>
          </motion.form>
        );
      case 'forgotPassword':
        return (
          <motion.form
            key="forgotPassword"
            variants={formVariants}
            initial="initial"
            animate="animate"
            exit="exit"
            transition={{ duration: 0.5, type: 'spring', bounce: 0.25 }}
            className="w-full flex flex-col gap-5 mb-8"
          >
            <h2 className="text-3xl font-extrabold mb-2 text-gray-800 tracking-tight">Reset Password</h2>
            <p className="text-gray-600 text-base mb-2">Enter your email and we'll send you a link to reset your password.</p>
            <input type="email" placeholder="Email Address" className="w-full px-5 py-3 rounded-2xl border border-gray-200 bg-white/80 shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-300 text-lg" />
            <button type="submit" className="w-full bg-gradient-to-r from-blue-600 via-purple-500 to-fuchsia-500 text-white py-3 rounded-2xl text-lg font-bold shadow-lg hover:scale-105 hover:shadow-xl transition-all duration-300">Send Reset Link</button>
          </motion.form>
        );
      default:
        return null;
    }
  };

  return (
    <>
      {renderForm()}
      <div className="mt-6 text-base text-gray-600">
        {form === 'login' && (
          <>
            <p className="mb-2">Don't have an account? <span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setForm('register')}>Sign Up</span></p>
            <p><span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setForm('forgotPassword')}>Forgot Password?</span></p>
          </>
        )}
        {form === 'register' && (
          <p>Already have an account? <span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setForm('login')}>Sign In</span></p>
        )}
        {form === 'forgotPassword' && (
          <p>Remember your password? <span className="text-blue-600 font-semibold cursor-pointer hover:underline" onClick={() => setForm('login')}>Sign In</span></p>
        )}
      </div>
    </>
  );
};

export default AuthForm;
