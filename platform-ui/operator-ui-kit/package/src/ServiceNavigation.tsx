import { useEffect, useRef, useState } from "react";
import type { Language } from "./modules";

export function NavigationConfirmation({ language, onStay, onLeave }: { language: Language; onStay: () => void; onLeave: () => void }) {
  const stay = useRef<HTMLButtonElement>(null);
  useEffect(() => { stay.current?.focus(); }, []);
  const ru = language === "ru";
  return <section className="panel navigation-confirmation" role="alert" aria-label={ru ? "Несохранённые изменения" : "Unsaved changes"}>
    <h3>{ru ? "Есть несохранённые изменения" : "There are unsaved changes"}</h3>
    <p>{ru ? "При переходе введённые данные будут потеряны. Сохранённые решения и история не изменятся." : "Navigating away discards the current input. Saved decisions and history remain unchanged."}</p>
    <div className="actions"><button ref={stay} className="quiet" onClick={onStay}>{ru ? "Остаться" : "Stay"}</button><button className="danger" onClick={onLeave}>{ru ? "Перейти без сохранения" : "Leave without saving"}</button></div>
  </section>;
}

/** In-page confirmation works in isolated frames without allowing browser modals. */
export function useGuardedNavigation({ language, isDirty, onDiscard }: { language: Language; isDirty: () => boolean; onDiscard: () => void }) {
  const [pending, setPending] = useState<(() => void)>();
  const previousFocus = useRef<HTMLElement | null>(null);
  const navigate = (action: () => void) => {
    if (pending) return;
    if (isDirty()) {
      previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setPending(() => action);
    } else { onDiscard(); action(); }
  };
  const stay = () => { setPending(undefined); if (previousFocus.current?.isConnected) previousFocus.current.focus(); };
  const leave = () => { if (!pending) return; setPending(undefined); onDiscard(); pending(); };
  return { navigate, confirmation: pending ? <NavigationConfirmation language={language} onStay={stay} onLeave={leave} /> : null };
}

export function ServiceNavigation<T extends string>({ items, selected, onSelect, label }: {
  items: readonly { id: T; label: string }[]; selected: T; onSelect: (value: T) => void; label: string;
}) {
  return <nav className="section-navigation" aria-label={label}>{items.map(item => <button key={item.id} className={selected === item.id ? "active" : ""} aria-current={selected === item.id ? "page" : undefined} onClick={() => onSelect(item.id)}>{item.label}</button>)}</nav>;
}
