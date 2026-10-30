"use client";
import {useEffect, useState, type FormEvent} from 'react';
import {apiFetch} from '@/lib/api';

export function AccountRecovery({mode}: {mode: 'verify-email' | 'forgot-password' | 'reset-password'}) {
  const [token, setToken] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  useEffect(() => {
    // Fragment never reaches proxy/access logs; consume only after an explicit submit.
    const value = new URLSearchParams(window.location.hash.slice(1)).get('token') || '';
    setToken(value);
    window.history.replaceState(null, '', window.location.pathname);
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || done) return;
    if (mode === 'reset-password' && password !== confirmation) { setError('Пароли не совпадают. Повторите ввод.'); return; }
    setBusy(true); setError(''); setMessage('');
    const endpoint = mode === 'verify-email' && !token ? 'resend-verification' : mode;
    try {
      const body = endpoint === 'reset-password' ? {token, password} : endpoint === 'verify-email' ? {token} : {email};
      const response = await apiFetch(`/api/v1/auth/${endpoint}`, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось выполнить действие. Проверьте ссылку или запросите новую.');
      setMessage(result.message); setPassword(''); setConfirmation('');
      setDone(endpoint === 'verify-email' || endpoint === 'reset-password');
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Сервер недоступен. Повторите позже.'); }
    finally { setBusy(false); }
  }
  const heading = mode === 'verify-email' ? 'Подтвердите email' : mode === 'forgot-password' ? 'Восстановить пароль' : 'Новый пароль';
  return <main className="auth-shell"><section className="auth-card">
    <a className="auth-brand" href="/">QuickList.</a><h1>{heading}</h1>
    <p className="auth-help">{mode === 'verify-email' ? 'До подтверждения адреса доступ к ассистенту закрыт. Откройте ссылку из письма или запросите новое.' : 'Одноразовая ссылка действует ограниченное время. Пароль в письме не передаётся.'}</p>
    <form onSubmit={submit}>
      {(mode === 'forgot-password' || (mode === 'verify-email' && !token)) && <label>Email
        <input type="email" autoComplete="email" maxLength={254} required value={email} onChange={e => setEmail(e.target.value)}/></label>}
      {mode === 'reset-password' && <>
        <label>Новый пароль<input type="password" autoComplete="new-password" required minLength={12} maxLength={128} value={password} onChange={e => setPassword(e.target.value)}/></label>
        <label>Повторите пароль<input type="password" autoComplete="new-password" required minLength={12} maxLength={128} value={confirmation} onChange={e => setConfirmation(e.target.value)}/></label>
      </>}
      {error && <p className="auth-error" role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      <button type="submit" className="button" disabled={busy || done || (mode === 'reset-password' && !token)}>
        {busy ? 'Подождите…' : mode === 'verify-email' && token ? 'Подтвердить email' : mode === 'reset-password' ? 'Сохранить пароль' : 'Отправить письмо'}
      </button>
    </form>
    <p className="auth-help"><a href="/login">Войти</a> · <a href="/forgot-password">Запросить восстановление</a></p>
  </section></main>;
}
