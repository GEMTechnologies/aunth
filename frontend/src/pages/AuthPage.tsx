
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

const testimonials = [
  {
    text: "Granada made our user management seamless. The SSO integration was incredibly smooth.",
    author: "Sarah Chen",
    role: "CTO at TechFlow",
    avatar: "https://images.unsplash.com/photo-1494790108755-2616b612b47c?w=64&h=64&fit=crop&crop=face"
  },
  {
    text: "Finally, an auth service that just works. No more worrying about security implementation.",
    author: "Marcus Rodriguez",
    role: "Lead Developer",
    avatar: "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=64&h=64&fit=crop&crop=face"
  },
  {
    text: "The developer experience is outstanding. Integration took less than an hour.",
    author: "Emily Johnson",
    role: "Full Stack Engineer",
    avatar: "https://images.unsplash.com/photo-1438761681033-6461ffad8d80?w=64&h=64&fit=crop&crop=face"
  },
  {
    text: "Scalable, secure, and simple. Granada handles everything we need for user authentication.",
    author: "David Park",
    role: "Engineering Manager",
    avatar: "https://images.unsplash.com/photo-1472099645785-5658abf4ff4e?w=64&h=64&fit=crop&crop=face"
  }
];

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
  const [currentTestimonial, setCurrentTestimonial] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setCurrentTestimonial((prev) => (prev + 1) % testimonials.length);
    }, 4000);
    return () => clearInterval(interval);
  }, []);

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
    <div className="min-h-screen flex bg-white">
      {/* Left Side - Auth Form */}
      <div className="flex-1 flex items-center justify-center px-4 sm:px-6 lg:px-8">
        <div className="max-w-md w-full space-y-8">
          {/* Logo */}
          <div className="text-center">
            <div className="flex items-center justify-center mb-6">
              <div className="w-8 h-8 bg-orange-500 rounded-lg flex items-center justify-center mr-3">
                <div className="w-4 h-4 bg-white rounded-sm"></div>
              </div>
              <h1 className="text-2xl font-bold text-gray-900">Granada</h1>
            </div>
          </div>

          <AnimatePresence mode="wait">
            <motion.div
              key={mode}
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -20 }}
              transition={{ duration: 0.3 }}
            >
              <h2 className="text-3xl font-bold text-gray-900 mb-2">
                {mode === "login" && "Welcome back"}
                {mode === "register" && "Create a Granada account"}
                {mode === "forgotPassword" && "Reset your password"}
              </h2>

              <p className="text-gray-600 mb-8">
                {mode === "login" && "Sign in to your account"}
                {mode === "register" && "Join thousands of developers building with Granada"}
                {mode === "forgotPassword" && "Enter your email to reset your password"}
              </p>

              {error && (
                <div className="mb-6 p-3 bg-red-50 border border-red-200 text-red-600 rounded-lg text-sm">
                  {error}
                </div>
              )}

              {/* Social Login Buttons */}
              {mode !== "forgotPassword" && (
                <div className="space-y-3 mb-6">
                  <SocialButton provider="google" />
                  <SocialButton provider="github" />
                  <button className="w-full flex items-center justify-center px-4 py-3 border border-gray-300 rounded-lg bg-white text-gray-700 text-sm font-medium hover:bg-gray-50 transition-colors">
                    <svg className="w-5 h-5 mr-3" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>
                    </svg>
                    Continue with X
                  </button>
                </div>
              )}

              {/* Or Divider */}
              {mode !== "forgotPassword" && (
                <div className="relative mb-6">
                  <div className="absolute inset-0 flex items-center">
                    <div className="w-full border-t border-gray-300" />
                  </div>
                  <div className="relative flex justify-center text-sm">
                    <span className="px-2 bg-white text-gray-500">Or</span>
                  </div>
                </div>
              )}

              {/* Email & Password Form */}
              <form onSubmit={handleSubmit} className="space-y-4">
                <div>
                  <label className="sr-only">Email address</label>
                  <input
                    type="email"
                    name="email"
                    placeholder="Email address"
                    value={formData.email}
                    onChange={handleChange}
                    required
                    className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-orange-500 focus:border-transparent"
                  />
                </div>

                {mode !== "forgotPassword" && (
                  <div>
                    <label className="sr-only">Password</label>
                    <input
                      type="password"
                      name="password"
                      placeholder="Password"
                      value={formData.password}
                      onChange={handleChange}
                      required
                      className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-orange-500 focus:border-transparent"
                    />
                  </div>
                )}

                {mode === "register" && (
                  <>
                    <div>
                      <label className="sr-only">Display Name</label>
                      <input
                        type="text"
                        name="displayName"
                        placeholder="Display Name"
                        value={formData.displayName}
                        onChange={handleChange}
                        className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-orange-500 focus:border-transparent"
                      />
                    </div>
                    <div>
                      <label className="sr-only">Confirm Password</label>
                      <input
                        type="password"
                        name="confirmPassword"
                        placeholder="Confirm Password"
                        value={formData.confirmPassword}
                        onChange={handleChange}
                        required
                        className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-orange-500 focus:border-transparent"
                      />
                    </div>
                  </>
                )}

                <button
                  type="submit"
                  disabled={loading}
                  className="w-full bg-orange-500 text-white py-3 rounded-lg font-medium hover:bg-orange-600 transition-colors disabled:opacity-50"
                >
                  {loading ? "Processing..." : (
                    mode === "login" ? "Continue with email & password" :
                    mode === "register" ? "Create account" : "Send reset email"
                  )}
                </button>
              </form>

              {/* SSO Option */}
              {mode !== "forgotPassword" && (
                <div className="mt-6 text-center">
                  <button className="text-sm text-orange-600 hover:text-orange-700 font-medium">
                    Or Single sign-on (SSO)
                  </button>
                </div>
              )}

              {/* Footer Links */}
              <div className="mt-8 space-y-2 text-center">
                {mode === "login" && (
                  <>
                    <p className="text-sm text-gray-600">
                      Don't have an account?{" "}
                      <button
                        onClick={() => setMode("register")}
                        className="text-orange-600 hover:text-orange-700 font-medium"
                      >
                        Sign up
                      </button>
                    </p>
                    <p className="text-sm text-gray-600">
                      <button
                        onClick={() => setMode("forgotPassword")}
                        className="text-orange-600 hover:text-orange-700 font-medium"
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
                      className="text-orange-600 hover:text-orange-700 font-medium"
                    >
                      Log in
                    </button>
                  </p>
                )}

                {mode === "forgotPassword" && (
                  <p className="text-sm text-gray-600">
                    Remember your password?{" "}
                    <button
                      onClick={() => setMode("login")}
                      className="text-orange-600 hover:text-orange-700 font-medium"
                    >
                      Sign in
                    </button>
                  </p>
                )}
              </div>

              {/* Terms */}
              <div className="mt-8 text-xs text-gray-500 text-center">
                By continuing, you agree to Granada's{" "}
                <a href="#" className="underline">Terms of Service</a>{" "}
                and <a href="#" className="underline">Privacy Policy</a>
              </div>
            </motion.div>
          </AnimatePresence>
        </div>
      </div>

      {/* Right Side - Hero Section with Testimonials */}
      <div className="hidden lg:flex lg:flex-1 lg:items-center lg:justify-center bg-gradient-to-br from-gray-900 via-gray-800 to-black relative overflow-hidden">
        {/* Animated Background Elements */}
        <div className="absolute inset-0">
          <div className="absolute top-20 left-20 w-64 h-64 bg-orange-500/10 rounded-full blur-xl animate-pulse"></div>
          <div className="absolute bottom-20 right-20 w-48 h-48 bg-blue-500/10 rounded-full blur-xl animate-pulse delay-1000"></div>
          <div className="absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 w-96 h-96 bg-purple-500/5 rounded-full blur-2xl animate-pulse delay-2000"></div>
        </div>

        {/* Content */}
        <div className="relative z-10 max-w-lg text-center text-white px-8">
          {/* Logo Animation */}
          <motion.div
            className="flex justify-center mb-8"
            initial={{ scale: 0, rotate: -180 }}
            animate={{ scale: 1, rotate: 0 }}
            transition={{ duration: 1, ease: "easeOut" }}
          >
            <div className="w-16 h-16 bg-orange-500 rounded-2xl flex items-center justify-center shadow-2xl">
              <div className="w-8 h-8 bg-white rounded-lg"></div>
            </div>
          </motion.div>

          {/* Main Headline */}
          <motion.h1
            className="text-5xl font-bold mb-6 bg-gradient-to-r from-white to-gray-300 bg-clip-text text-transparent"
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.3 }}
          >
            Idea to app, fast
          </motion.h1>

          {/* Subtitle */}
          <motion.p
            className="text-xl text-gray-300 mb-12 leading-relaxed"
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.5 }}
          >
            Build, deploy, and scale your applications with Granada's powerful authentication platform
          </motion.p>

          {/* Testimonials */}
          <motion.div
            className="bg-white/5 backdrop-blur-lg rounded-2xl p-6 border border-white/10"
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.7 }}
          >
            <AnimatePresence mode="wait">
              <motion.div
                key={currentTestimonial}
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                transition={{ duration: 0.5 }}
                className="space-y-4"
              >
                <p className="text-gray-200 italic leading-relaxed">
                  "{testimonials[currentTestimonial].text}"
                </p>
                <div className="flex items-center justify-center space-x-3">
                  <img
                    src={testimonials[currentTestimonial].avatar}
                    alt={testimonials[currentTestimonial].author}
                    className="w-10 h-10 rounded-full object-cover"
                  />
                  <div>
                    <div className="font-medium text-white">
                      {testimonials[currentTestimonial].author}
                    </div>
                    <div className="text-sm text-gray-400">
                      {testimonials[currentTestimonial].role}
                    </div>
                  </div>
                </div>
              </motion.div>
            </AnimatePresence>

            {/* Testimonial Indicators */}
            <div className="flex justify-center space-x-2 mt-6">
              {testimonials.map((_, index) => (
                <button
                  key={index}
                  onClick={() => setCurrentTestimonial(index)}
                  className={`w-2 h-2 rounded-full transition-colors ${
                    index === currentTestimonial ? 'bg-orange-500' : 'bg-white/30'
                  }`}
                />
              ))}
            </div>
          </motion.div>

          {/* Stats */}
          <motion.div
            className="mt-12 grid grid-cols-3 gap-8 text-center"
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.9 }}
          >
            <div>
              <div className="text-2xl font-bold text-orange-400">10k+</div>
              <div className="text-sm text-gray-400">Developers</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-orange-400">99.9%</div>
              <div className="text-sm text-gray-400">Uptime</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-orange-400">50M+</div>
              <div className="text-sm text-gray-400">Requests/day</div>
            </div>
          </motion.div>
        </div>
      </div>
    </div>
  );
};

export default AuthPage;
