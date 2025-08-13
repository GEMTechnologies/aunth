import React, { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { apiLogin, apiRegister, apiMe, apiRefresh } from "../lib/api";

type Mode = "login" | "register";

const Input: React.FC<React.InputHTMLAttributes<HTMLInputElement> & {label: string}> = ({label, ...props}) => (
  <label className="block">
    <span className="text-sm font-medium">{label}</span>
    <input {...props} className="mt-1 w-full rounded-2xl border px-4 py-2 outline-none focus:ring focus:ring-offset-1" />
  </label>
);

const Button: React.FC<React.ButtonHTMLAttributes<HTMLButtonElement>> = ({children, ...props}) => (
  <button {...props} className="w-full rounded-2xl px-4 py-2 font-semibold shadow hover:shadow-md transition disabled:opacity-50" />
);

const Card: React.FC<React.HTMLAttributes<HTMLDivElement>> = ({children, ...props}) => (
  <div {...props} className="rounded-3xl border bg-white/80 backdrop-blur p-6 shadow-sm" />
);

const AuthPage: React.FC = () => {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [me, setMe] = useState<any>(null);

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
            // noop
          }
        }
      });
    }
  }, []);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (mode === "login") {
        const t = await apiLogin(email, password);
        sessionStorage.setItem("access", t.access_token);
        localStorage.setItem("refresh", t.refresh_token);
        setMe(await apiMe(t.access_token));
      } else {
        await apiRegister(email, password, fullName);
        const t = await apiLogin(email, password);
        sessionStorage.setItem("access", t.access_token);
        localStorage.setItem("refresh", t.refresh_token);
        setMe(await apiMe(t.access_token));
      }
    } catch (err: any) {
      setError(err.message || "Something went wrong");
    } finally {
      setLoading(false);
    }
  };

  const logout = () => {
    sessionStorage.removeItem("access");
    localStorage.removeItem("refresh");
    setMe(null);
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-gray-50 via-blue-50 to-indigo-100">
      <div className="max-w-6xl w-full grid md:grid-cols-2 gap-8 p-4 md:p-8">
        <motion.div initial={{opacity:0,y:20}} animate={{opacity:1,y:0}} transition={{duration:0.5}} className="flex flex-col justify-center">
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight">
            Granada <span className="text-indigo-600">Authentication</span>
          </h1>
          <p className="mt-4 text-gray-600 text-lg">
            Secure, scalable identity for organizations, students, NGOs, and businesses.
          </p>
          <ul className="mt-6 text-gray-700 space-y-2">
            <li>• Email/password with JWT access & refresh</li>
            <li>• Organizations & roles ready for dashboards</li>
            <li>• API-first, easy to integrate with your frontend</li>
          </ul>
        </motion.div>

        <motion.div initial={{opacity:0,y:20}} animate={{opacity:1,y:0}} transition={{duration:0.5, delay:0.1}}>
          <Card>
            {!me ? (
              <>
                <div className="flex rounded-2xl bg-gray-100 p-1 mb-6">
                  <button className={`flex-1 rounded-2xl px-3 py-2 font-semibold ${mode==="login" ? "bg-white shadow" : ""}`} onClick={()=>setMode("login")}>Login</button>
                  <button className={`flex-1 rounded-2xl px-3 py-2 font-semibold ${mode==="register" ? "bg-white shadow" : ""}`} onClick={()=>setMode("register")}>Register</button>
                </div>

                <form onSubmit={onSubmit} className="space-y-4">
                  {mode === "register" && (
                    <Input label="Full name" placeholder="Jane Doe" value={fullName} onChange={e=>setFullName(e.target.value)} />
                  )}
                  <Input label="Email" type="email" placeholder="you@example.com" value={email} onChange={e=>setEmail(e.target.value)} />
                  <Input label="Password" type="password" placeholder="********" value={password} onChange={e=>setPassword(e.target.value)} />
                  {error && <p className="text-sm text-red-600">{error}</p>}
                  <Button disabled={loading} type="submit">{loading ? "Please wait..." : (mode === "login" ? "Sign in" : "Create account")}</Button>
                </form>
              </>
            ) : (
              <div className="space-y-4">
                <h2 className="text-2xl font-bold">Welcome</h2>
                <div className="rounded-xl border p-4 bg-white">
                  <p><span className="font-semibold">Email:</span> {me.email}</p>
                  <p><span className="font-semibold">Name:</span> {me.full_name || "—"}</p>
                  <p><span className="font-semibold">Verified:</span> {me.is_verified ? "Yes" : "No"}</p>
                </div>
                <div className="flex gap-3">
                  <Button onClick={logout}>Log out</Button>
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
