import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Owner-local details; no access to the enclosing platform's DOM or tokens. */
export function EvidenceDialog({ id, title, onClose, children, language }: {
  id: string; title: string; onClose: () => void; children: ReactNode; language: "ru" | "en";
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const root = document.documentElement;
    const overflow = root.style.overflow;
    const padding = root.style.paddingRight;
    const scrollbar = window.innerWidth - root.clientWidth;
    if (scrollbar > 0) root.style.paddingRight = `${parseFloat(getComputedStyle(root).paddingRight) + scrollbar}px`;
    root.style.overflow = "hidden";
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry || entry.intersectionRect.height < 80) return;
      element.style.top = `${entry.intersectionRect.top + 16}px`;
      element.style.maxHeight = `${entry.intersectionRect.height - 32}px`;
      if (!element.open) element.showModal();
    });
    const measure = () => { observer.unobserve(root); observer.observe(root); };
    const visibility = new IntersectionObserver(([entry]) => {
      if (element.open && entry && entry.intersectionRatio < .999) measure();
    }, { threshold: 1 });
    visibility.observe(element);
    measure();
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect(); visibility.disconnect(); window.removeEventListener("resize", measure);
      element.close(); root.style.overflow = overflow; root.style.paddingRight = padding;
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, []);
  return createPortal(<dialog ref={dialog} id={id} className="evidence-dialog" aria-labelledby={`${id}-title`}
    onClose={onClose} onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }}>
    <header><h2 id={`${id}-title`}>{title}</h2><button className="quiet compact" onClick={onClose}>{language === "ru" ? "Закрыть" : "Close"}</button></header>
    <div className="evidence-dialog-content">{children}</div>
  </dialog>, document.body);
}
