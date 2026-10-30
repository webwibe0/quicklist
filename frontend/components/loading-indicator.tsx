export function LoadingIndicator() {
  return <div className="thinking" role="status" aria-live="polite">
    <span className="thinking-dots" aria-hidden="true"><i /><i /><i /></span>
    <span>Разбираю запрос…</span>
  </div>;
}
