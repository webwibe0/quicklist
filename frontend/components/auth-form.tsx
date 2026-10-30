"use client";
import { useEffect, useState, type FormEvent } from "react";
import { apiFetch } from "@/lib/api";

export function AuthForm({mode}: {mode: "login" | "register"}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    fetch("/api/v1/auth/csrf", {cache: "no-store", credentials: "same-origin"})
      .then(response => {if (!response.ok) throw new Error(); setReady(true);})
      .catch(() => setError("Сервер временно недоступен. Обновите страницу."));
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !ready) return;
    setBusy(true); setError("");
    try {
      const response = await apiFetch(`/api/v1/auth/${mode}`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({email, password}),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail :
        Array.isArray(body.detail) ? body.detail[0]?.msg : "Не удалось войти.");
      setPassword("");
      window.location.replace(body.verification_required || body.access_allowed === false ? '/verify-email' : '/');
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Не удалось войти. Повторите запрос.");
    } finally { setBusy(false); }
  }
  return <main className="auth-shell"><section className="auth-card">
    <a className="auth-brand" href="/">QuickList.</a>
    <p className="auth-kicker">Ваше личное пространство</p>
    <h1>{mode === "login" ? "С возвращением" : "Создать аккаунт"}</h1>
    <form onSubmit={submit}>
      <label>Email<input type="email" autoComplete="username" required maxLength={254}
        value={email} onChange={event => setEmail(event.target.value)} /></label>
      <label>Пароль<input type="password" autoComplete={mode === "login" ? "current-password" : "new-password"}
        required minLength={12} maxLength={128} value={password} onChange={event => setPassword(event.target.value)} /></label>
      {mode === "register" && <p className="auth-help">От 12 до 128 символов.</p>}
      {error && <p className="auth-error" role="alert">{error}</p>}
      <button className="button" type="submit" disabled={busy || !ready}>
        {busy ? "Подождите…" : mode === "login" ? "Войти" : "Зарегистрироваться"}
      </button>
    </form>
    {mode === 'login' && <p className="auth-help"><a href="/forgot-password">Забыли пароль?</a></p>}
    <p className="auth-help">{mode === "login" ? "Нет аккаунта? " : "Уже зарегистрированы? "}
      <a href={mode === "login" ? "/register" : "/login"}>{mode === "login" ? "Регистрация" : "Войти"}</a></p>
  </section></main>;
}
