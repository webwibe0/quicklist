import type { ReactNode } from "react";
import { Icon } from "./icons";
import { QuickListLogo } from "./quicklist-logo";

export type ConnectionStatus = {text: string; tone: "waiting" | "online" | "offline" | "test"};

export function Header({status, disabled, onNewChat, today, account, onLogout, logoutBusy = false, calendarControls}: {calendarControls?: ReactNode; account?: {email: string; email_verified?: boolean}; onLogout?: () => void; logoutBusy?: boolean; status: ConnectionStatus; disabled: boolean; onNewChat: () => void; today?: string}) {
  return <header className="app-header">
    <div className="brand" aria-label="QuickList Assistant">
      <span className="brand-mark"><QuickListLogo /></span>
      <div><span className="brand-name">QuickList<span className="brand-period">.</span></span><span className="brand-subtitle">PERSONAL AI ASSISTANT</span></div>
    </div>
    <div className="header-day">{today && <><span>Ваш день, в фокусе</span><time dateTime={today}>{new Intl.DateTimeFormat("ru-RU", {
      weekday: "long", day: "numeric", month: "long", timeZone: "UTC",
    }).format(new Date(`${today}T12:00:00Z`))}</time></>}
      <span className={`connection-status status-${status.tone}`} role="status"><i />{status.text}</span>
    </div>
    <div className="header-actions">
      {calendarControls}
      {account && <div className="header-account">
      <span className="profile-monogram" aria-hidden="true">{account.email.slice(0,2).toUpperCase()}</span>
      {account && <span className="account-controls"><span title={account.email}>{account.email}</span>{account.email_verified === false && <a href="/verify-email">Подтвердить email</a>}{account.email_verified === true && <small>Email подтверждён</small>}<button type="button" onClick={onLogout} disabled={logoutBusy}>Выйти</button></span>}
      </div>}
      <button type="button" className="button button-outline new-chat-button" disabled={disabled} onClick={onNewChat}
        title="Очистить чат на этом устройстве. Сохранённые события останутся.">
        <Icon name="plus" width="17" height="17" /><span>Новый диалог</span>
      </button>
    </div>
  </header>;
}
