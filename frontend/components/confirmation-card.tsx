import type { AssistantResponse } from "@/types/assistant";
import { Icon } from "./icons";

export function ConfirmationCard({text, action, active, disabled, onReply}: {
  text: string; action?: AssistantResponse["action"]; active: boolean; disabled: boolean;
  onReply: (answer: string) => void;
}) {
  const deleting = action === "DELETE_EVENT";
  const label = deleting ? "Удалить" : action === "CREATE_EVENT" ? "Создать" : "Подтвердить";
  return <div className={`confirmation-card${!active ? " confirmation-history" : ""}`} role="group" aria-label="Подтверждение действия">
    <div className="confirmation-label"><Icon name={deleting ? "trash" : "check"} width="16" height="16" />Нужно ваше подтверждение</div>
    <h3 className="confirmation-title">{deleting ? "Удалить событие?" : action === "CREATE_EVENT" ? "Подтвердить создание" : "Всё верно?"}</h3>
    <p className="message-text">{text}</p>
    <div className="confirmation-actions">
      <button type="button" className={`button ${deleting ? "button-danger" : "button-primary"}`}
        disabled={!active || disabled} onClick={() => onReply("да")}>{label}</button>
      <button type="button" className="button button-outline" disabled={!active || disabled} onClick={() => onReply("нет")}>Отмена</button>
    </div>
    {!active && <p className="confirmation-note">Предыдущий запрос · продолжение диалога ниже</p>}
  </div>;
}
