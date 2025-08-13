import React, { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { apiLogin, apiRegister, apiMe, apiRefresh } from "../lib/api";
import SocialButton from "../components/SocialButton";

type Mode = "login" | "register" | "forgotPassword";

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

interface AuthPageProps {
  onLogin: (user: User) => void;
}

interface AuthFormData {
  email: string;
  password: string;
  displayName?: string;
  confirmPassword?: string;
}

const AuthPage: React.FC<AuthPageProps> = ({ onLogin }) => {
  const [mode, setMode] = useState<Mode>("login");
  const [formData, setFormData] = useState<AuthFormData>({
    email: "",
    password: "",
    displayName: "",
    confirmPassword: "",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      if (mode === "register") {
        if (formData.password !== formData.confirmPassword) {
          throw new Error("Passwords don't match");
        }
        const response = await apiRegister(
          formData.email,
          formData.password,
          formData.displayName || ""
        );
        localStorage.setItem("access_token", response.access_token);
        localStorage.setItem("refresh_token", response.refresh_token);
        const user = await apiMe();
        onLogin(user);
      } else if (mode === "login") {
        const response = await apiLogin(formData.email, formData.password);
        localStorage.setItem("access_token", response.access_token);
        localStorage.setItem("refresh_token", response.refresh_token);
        const user = await apiMe();
        onLogin(user);
      }
    } catch (err: any) {
      setError(err.message || "An error occurred");
    } finally {
      setLoading(false);
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormData({
      ...formData,
      [e.target.name]: e.target.value,
    });
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-purple-600 to-blue-500 font-sans text-gray-800">
      <div className="max-w-lg w-full bg-white/80 backdrop-blur-lg p-10 rounded-3xl shadow-2xl text-center border border-white/40 mx-4 transition-all duration-300">
        <img
          src="https://via.placeholder.com/120x50.png?text=Granada"
          alt="Granada Logo"
          className="max-w-[120px] mx-auto mb-8 drop-shadow-lg"
        />

        <AnimatePresence mode="wait">
          <motion.div
            key={mode}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            transition={{ duration: 0.3 }}
          >
            <h1 className="text-3xl font-extrabold text-gray-900 mb-2 tracking-tight">
              {mode === "login" && "Welcome Back"}
              {mode === "register" && "Create Account"}
              {mode === "forgotPassword" && "Reset Password"}
            </h1>

            <p className="text-gray-600 mb-8">
              {mode === "login" && "Sign in to your Granada account"}
              {mode === "register" && "Join the Granada platform"}
              {mode === "forgotPassword" && "Enter your email to reset password"}
            </p>

            {error && (
              <div className="mb-4 p-3 bg-red-100 border border-red-400 text-red-700 rounded">
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4">
              <input
                type="email"
                name="email"
                placeholder="Email address"
                value={formData.email}
                onChange={handleChange}
                required
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />

              {mode !== "forgotPassword" && (
                <input
                  type="password"
                  name="password"
                  placeholder="Password"
                  value={formData.password}
                  onChange={handleChange}
                  required
                  className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              )}

              {mode === "register" && (
                <>
                  <input
                    type="text"
                    name="displayName"
                    placeholder="Display Name"
                    value={formData.displayName}
                    onChange={handleChange}
                    className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                  <input
                    type="password"
                    name="confirmPassword"
                    placeholder="Confirm Password"
                    value={formData.confirmPassword}
                    onChange={handleChange}
                    required
                    className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </>
              )}

              <button
                type="submit"
                disabled={loading}
                className="w-full bg-blue-600 text-white py-3 rounded-lg font-semibold hover:bg-blue-700 transition-colors disabled:opacity-50"
              >
                {loading ? "Processing..." : (
                  mode === "login" ? "Sign In" :
                  mode === "register" ? "Create Account" : "Send Reset Link"
                )}
              </button>
            </form>

            {/* Social Login */}
            {mode !== "forgotPassword" && (
              <div className="mt-6">
                <div className="relative">
                  <div className="absolute inset-0 flex items-center">
                    <div className="w-full border-t border-gray-300" />
                  </div>
                  <div className="relative flex justify-center text-sm">
                    <span className="px-2 bg-white text-gray-500">Or continue with</span>
                  </div>
                </div>
                
                <div className="mt-6 space-y-3">
                  <SocialButton provider="google" />
                  <SocialButton provider="github" />
                </div>
              </div>
            )}

            <div className="mt-6 space-y-2">
              {mode === "login" && (
                <>
                  <p className="text-sm text-gray-600">
                    Don't have an account?{" "}
                    <button
                      onClick={() => setMode("register")}
                      className="text-blue-600 hover:text-blue-700 font-medium"
                    >
                      Sign up
                    </button>
                  </p>
                  <p className="text-sm text-gray-600">
                    <button
                      onClick={() => setMode("forgotPassword")}
                      className="text-blue-600 hover:text-blue-700 font-medium"
                    >
                      Forgot password?
                    </button>
                  </p>
                </>
              )}

              {mode === "register" && (
                <p className="text-sm text-gray-600">
                  Already have an account?{" "}
                  <button
                    onClick={() => setMode("login")}
                    className="text-blue-600 hover:text-blue-700 font-medium"
                  >
                    Sign in
                  </button>
                </p>
              )}

              {mode === "forgotPassword" && (
                <p className="text-sm text-gray-600">
                  Remember your password?{" "}
                  <button
                    onClick={() => setMode("login")}
                    className="text-blue-600 hover:text-blue-700 font-medium"
                  >
                    Sign in
                  </button>
                </p>
              )}
            </div>
          </motion.div>
        </AnimatePresence>

        <div className="mt-6 text-xs text-gray-400">
          Granada Platform &copy; {new Date().getFullYear()}
        </div>
      </div>
    </div>
  );
};

export default AuthPage;