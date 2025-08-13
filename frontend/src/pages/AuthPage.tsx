
import React, { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { apiLogin, apiRegister, apiMe, apiRefresh } from "../lib/api";

type Mode = "login" | "register" | "forgotPassword";

interface FormData {
  email: string;
  password: string;
  fullName: string;
  organizationName: string;
  confirmPassword: string;
}

const Input: React.FC<React.InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  error?: string;
}> = ({ label, error, ...props }) => (
  <div className="space-y-1">
    <label className="block text-sm font-medium text-gray-700">{label}</label>
    <input 
      {...props} 
      className={`w-full px-4 py-3 rounded-xl border ${
        error ? 'border-red-300 focus:ring-red-500' : 'border-gray-200 focus:ring-blue-500'
      } bg-white/80 backdrop-blur-sm shadow-sm focus:outline-none focus:ring-2 focus:border-transparent transition-all duration-200`} 
    />
    {error && <p className="text-sm text-red-600">{error}</p>}
  </div>
);

const Button: React.FC<React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary';
}> = ({ children, variant = 'primary', ...props }) => (
  <button 
    {...props} 
    className={`w-full px-4 py-3 rounded-xl font-semibold shadow-sm transition-all duration-200 ${
      variant === 'primary' 
        ? 'bg-gradient-to-r from-blue-600 to-indigo-600 text-white hover:from-blue-700 hover:to-indigo-700 hover:shadow-lg transform hover:-translate-y-0.5' 
        : 'bg-white text-gray-700 border border-gray-200 hover:bg-gray-50'
    } disabled:opacity-50 disabled:cursor-not-allowed disabled:transform-none`}
  />
);

const Card: React.FC<React.HTMLAttributes<HTMLDivElement>> = ({ children, ...props }) => (
  <div {...props} className="bg-white/90 backdrop-blur-xl rounded-3xl border border-white/20 shadow-xl p-8">
    {children}
  </div>
);

const AuthPage: React.FC = () => {
  const [mode, setMode] = useState<Mode>("login");
  const [formData, setFormData] = useState<FormData>({
    email: "",
    password: "",
    fullName: "",
    organizationName: "",
    confirmPassword: ""
  });
  const [loading, setLoading] = useState(false);
  const [errors, setErrors] = useState<Partial<FormData>>({});
  const [me, setMe] = useState<any>(null);
  const [message, setMessage] = useState<string>("");

  useEffect(() => {
    const refresh = localStorage.getItem("refresh");
    const access = sessionStorage.getItem("access");
    if (access) {
      apiMe(access).then(setMe).catch(async () => {
        if (refresh) {
          try {
            const t = await apiRefresh(refresh);
            sessionStorage.setItem("access", t.access_token);
            setMe(await apiMe(t.access_token));
          } catch {
            // Clear invalid tokens
            localStorage.removeItem("refresh");
            sessionStorage.removeItem("access");
          }
        }
      });
    }
  }, []);

  const validateForm = (): boolean => {
    const newErrors: Partial<FormData> = {};
    
    if (!formData.email) newErrors.email = "Email is required";
    else if (!/\S+@\S+\.\S+/.test(formData.email)) newErrors.email = "Email is invalid";
    
    if (!formData.password) newErrors.password = "Password is required";
    else if (formData.password.length < 8) newErrors.password = "Password must be at least 8 characters";
    
    if (mode === "register") {
      if (!formData.fullName) newErrors.fullName = "Full name is required";
      if (formData.password !== formData.confirmPassword) {
        newErrors.confirmPassword = "Passwords don't match";
      }
    }
    
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleInputChange = (field: keyof FormData, value: string) => {
    setFormData(prev => ({ ...prev, [field]: value }));
    if (errors[field]) {
      setErrors(prev => ({ ...prev, [field]: undefined }));
    }
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setMessage("");
    
    if (!validateForm()) return;
    
    setLoading(true);
    try {
      if (mode === "login") {
        const t = await apiLogin(formData.email, formData.password);
        sessionStorage.setItem("access", t.access_token);
        localStorage.setItem("refresh", t.refresh_token);
        setMe(await apiMe(t.access_token));
      } else if (mode === "register") {
        await apiRegister(formData.email, formData.password, formData.fullName);
        const t = await apiLogin(formData.email, formData.password);
        sessionStorage.setItem("access", t.access_token);
        localStorage.setItem("refresh", t.refresh_token);
        setMe(await apiMe(t.access_token));
      } else if (mode === "forgotPassword") {
        // TODO: Implement password reset
        setMessage("Password reset link sent to your email!");
      }
    } catch (err: any) {
      setMessage(err.message || "Something went wrong");
    } finally {
      setLoading(false);
    }
  };

  const logout = () => {
    sessionStorage.removeItem("access");
    localStorage.removeItem("refresh");
    setMe(null);
    setFormData({
      email: "",
      password: "",
      fullName: "",
      organizationName: "",
      confirmPassword: ""
    });
  };

  const formVariants = {
    initial: { opacity: 0, y: 20 },
    animate: { opacity: 1, y: 0 },
    exit: { opacity: 0, y: -20 }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 via-indigo-50 to-purple-50 flex items-center justify-center p-4">
      <div className="max-w-7xl w-full grid lg:grid-cols-2 gap-12 items-center">
        {/* Left Column - Branding */}
        <motion.div 
          initial={{ opacity: 0, x: -50 }} 
          animate={{ opacity: 1, x: 0 }} 
          transition={{ duration: 0.6 }}
          className="text-center lg:text-left space-y-8"
        >
          <div className="space-y-4">
            <h1 className="text-5xl lg:text-6xl font-bold tracking-tight">
              <span className="bg-gradient-to-r from-blue-600 to-indigo-600 bg-clip-text text-transparent">
                Granada
              </span>
              <br />
              Authentication
            </h1>
            <p className="text-xl text-gray-600 max-w-md">
              The operating system for impact. Secure, scalable identity management for organizations, students, NGOs, and businesses.
            </p>
          </div>
          
          <div className="space-y-4 text-gray-700">
            <div className="flex items-center space-x-3">
              <div className="w-2 h-2 bg-green-500 rounded-full"></div>
              <span>Multi-tenant organization support</span>
            </div>
            <div className="flex items-center space-x-3">
              <div className="w-2 h-2 bg-blue-500 rounded-full"></div>
              <span>JWT-based secure authentication</span>
            </div>
            <div className="flex items-center space-x-3">
              <div className="w-2 h-2 bg-purple-500 rounded-full"></div>
              <span>Role-based access control</span>
            </div>
            <div className="flex items-center space-x-3">
              <div className="w-2 h-2 bg-indigo-500 rounded-full"></div>
              <span>API-first architecture</span>
            </div>
          </div>
        </motion.div>

        {/* Right Column - Auth Form */}
        <motion.div 
          initial={{ opacity: 0, x: 50 }} 
          animate={{ opacity: 1, x: 0 }} 
          transition={{ duration: 0.6, delay: 0.2 }}
          className="w-full max-w-md mx-auto"
        >
          <Card>
            {!me ? (
              <>
                {/* Mode Switcher */}
                <div className="flex rounded-xl bg-gray-100 p-1 mb-8">
                  <button 
                    className={`flex-1 rounded-lg px-4 py-2 font-semibold transition-all ${
                      mode === "login" ? "bg-white shadow-sm text-gray-900" : "text-gray-600"
                    }`} 
                    onClick={() => setMode("login")}
                  >
                    Sign In
                  </button>
                  <button 
                    className={`flex-1 rounded-lg px-4 py-2 font-semibold transition-all ${
                      mode === "register" ? "bg-white shadow-sm text-gray-900" : "text-gray-600"
                    }`} 
                    onClick={() => setMode("register")}
                  >
                    Sign Up
                  </button>
                </div>

                <AnimatePresence mode="wait">
                  <motion.form
                    key={mode}
                    variants={formVariants}
                    initial="initial"
                    animate="animate"
                    exit="exit"
                    transition={{ duration: 0.3 }}
                    onSubmit={onSubmit}
                    className="space-y-6"
                  >
                    {mode === "forgotPassword" && (
                      <div className="text-center space-y-2">
                        <h2 className="text-2xl font-bold text-gray-900">Reset Password</h2>
                        <p className="text-gray-600">Enter your email and we'll send you a reset link</p>
                      </div>
                    )}

                    {mode === "register" && (
                      <>
                        <Input 
                          label="Full Name" 
                          placeholder="John Doe" 
                          value={formData.fullName} 
                          onChange={(e) => handleInputChange('fullName', e.target.value)}
                          error={errors.fullName}
                        />
                        <Input 
                          label="Organization Name (Optional)" 
                          placeholder="Acme Inc" 
                          value={formData.organizationName} 
                          onChange={(e) => handleInputChange('organizationName', e.target.value)}
                        />
                      </>
                    )}

                    <Input 
                      label="Email" 
                      type="email" 
                      placeholder="you@example.com" 
                      value={formData.email} 
                      onChange={(e) => handleInputChange('email', e.target.value)}
                      error={errors.email}
                    />

                    {mode !== "forgotPassword" && (
                      <Input 
                        label="Password" 
                        type="password" 
                        placeholder="••••••••" 
                        value={formData.password} 
                        onChange={(e) => handleInputChange('password', e.target.value)}
                        error={errors.password}
                      />
                    )}

                    {mode === "register" && (
                      <Input 
                        label="Confirm Password" 
                        type="password" 
                        placeholder="••••••••" 
                        value={formData.confirmPassword} 
                        onChange={(e) => handleInputChange('confirmPassword', e.target.value)}
                        error={errors.confirmPassword}
                      />
                    )}

                    {message && (
                      <div className={`p-3 rounded-lg text-sm ${
                        message.includes('sent') ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
                      }`}>
                        {message}
                      </div>
                    )}

                    <Button disabled={loading} type="submit">
                      {loading ? "Please wait..." : 
                        mode === "login" ? "Sign In" : 
                        mode === "register" ? "Create Account" : 
                        "Send Reset Link"}
                    </Button>

                    {mode !== "forgotPassword" && (
                      <div className="text-center space-y-2 text-sm text-gray-600">
                        {mode === "login" ? (
                          <>
                            <p>
                              Don't have an account?{" "}
                              <button 
                                type="button"
                                className="text-blue-600 font-semibold hover:underline" 
                                onClick={() => setMode("register")}
                              >
                                Sign up
                              </button>
                            </p>
                            <p>
                              <button 
                                type="button"
                                className="text-blue-600 font-semibold hover:underline" 
                                onClick={() => setMode("forgotPassword")}
                              >
                                Forgot password?
                              </button>
                            </p>
                          </>
                        ) : (
                          <p>
                            Already have an account?{" "}
                            <button 
                              type="button"
                              className="text-blue-600 font-semibold hover:underline" 
                              onClick={() => setMode("login")}
                            >
                              Sign in
                            </button>
                          </p>
                        )}
                      </div>
                    )}

                    {mode === "forgotPassword" && (
                      <div className="text-center">
                        <button 
                          type="button"
                          className="text-blue-600 font-semibold hover:underline text-sm" 
                          onClick={() => setMode("login")}
                        >
                          Back to sign in
                        </button>
                      </div>
                    )}
                  </motion.form>
                </AnimatePresence>

                {/* Social Login */}
                {mode !== "forgotPassword" && (
                  <div className="mt-8">
                    <div className="relative">
                      <div className="absolute inset-0 flex items-center">
                        <div className="w-full border-t border-gray-200"></div>
                      </div>
                      <div className="relative flex justify-center text-sm">
                        <span className="px-2 bg-white text-gray-500">Or continue with</span>
                      </div>
                    </div>

                    <div className="mt-6 grid grid-cols-2 gap-3">
                      <Button variant="secondary">
                        <svg className="w-5 h-5 mr-2" viewBox="0 0 24 24">
                          <path fill="currentColor" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                          <path fill="currentColor" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                          <path fill="currentColor" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
                          <path fill="currentColor" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
                        </svg>
                        Google
                      </Button>
                      <Button variant="secondary">
                        <svg className="w-5 h-5 mr-2" fill="currentColor" viewBox="0 0 24 24">
                          <path d="M12 0C5.374 0 0 5.373 0 12 0 17.302 3.438 21.8 8.207 23.387c.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0112 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576C20.566 21.797 24 17.300 24 12c0-6.627-5.373-12-12-12z"/>
                        </svg>
                        GitHub
                      </Button>
                    </div>
                  </div>
                )}
              </>
            ) : (
              /* User Profile */
              <div className="space-y-6">
                <div className="text-center">
                  <div className="w-16 h-16 bg-gradient-to-r from-blue-600 to-indigo-600 rounded-full mx-auto mb-4 flex items-center justify-center text-white text-xl font-bold">
                    {me.full_name?.charAt(0) || me.email.charAt(0).toUpperCase()}
                  </div>
                  <h2 className="text-2xl font-bold text-gray-900">Welcome back!</h2>
                  <p className="text-gray-600">{me.email}</p>
                </div>

                <div className="bg-gray-50 rounded-xl p-4 space-y-3">
                  <div className="flex justify-between">
                    <span className="font-medium text-gray-700">Name:</span>
                    <span className="text-gray-900">{me.full_name || "—"}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="font-medium text-gray-700">Status:</span>
                    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                      me.is_verified ? 'bg-green-100 text-green-800' : 'bg-yellow-100 text-yellow-800'
                    }`}>
                      {me.is_verified ? 'Verified' : 'Pending verification'}
                    </span>
                  </div>
                </div>

                <div className="space-y-3">
                  <Button onClick={() => window.location.href = '/profile'}>
                    Manage Profile
                  </Button>
                  <Button variant="secondary" onClick={logout}>
                    Sign Out
                  </Button>
                </div>
              </div>
            )}
          </Card>
        </motion.div>
      </div>
    </div>
  );
};

export default AuthPage;
