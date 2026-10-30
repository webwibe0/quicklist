import { Icon, type IconName } from "./icons";

const examples: {text: string; icon: IconName}[] = [
  {text: "Что у меня завтра?", icon: "calendar"},
  {text: "Напомни завтра вечером позвонить Ивану", icon: "bell"},
  {text: "Перенеси встречу с командой на час позже", icon: "clock"},
  {text: "Покажи планы на неделю", icon: "task"},
];

export function EmptyState({onExample, disabled}: {onExample: (text: string) => void; disabled: boolean}) {
  return <div className="empty-state">
    <div className="hero-landscape" aria-hidden="true"><span className="hero-sun" /><span className="hero-orbit" /><i className="hero-orbit-dot" /><div className="hero-mountains" /></div>
    <p className="eyebrow">Больше, чем ответы</p>
    <h1>Скажите как есть.<br /><span>Я разберусь.</span></h1>
    <p className="welcome-copy">Задачи, встречи, напоминания, идеи.<br />Дата, время и уточнения — обычным языком.</p>
    <div className="example-grid" aria-label="Примеры запросов">
      {examples.map(example => <button className="example-chip" type="button" key={example.text}
        disabled={disabled} onClick={() => onExample(example.text)}>
        <span className="example-icon"><Icon name={example.icon} width="18" height="18" /></span>
        <span>{example.text}</span><Icon name="arrow" className="example-arrow" width="16" height="16" />
      </button>)}
    </div>
    <p className="welcome-hint">Выберите пример или напишите то, что нужно — своими словами.</p>
  </div>;
}
