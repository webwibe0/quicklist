import type { ReactNode } from "react";

export function AppShell({header, children, sidebar}: {header: ReactNode; children: ReactNode; sidebar: ReactNode}) {
  return <div className="app-shell">
    <a className="skip-link" href="#message">Перейти к сообщению</a>
    {header}
    <main className="workspace">
      <section className="chat-panel" aria-label="Диалог с ассистентом">{children}</section>
      {sidebar}
    </main>
  </div>;
}
