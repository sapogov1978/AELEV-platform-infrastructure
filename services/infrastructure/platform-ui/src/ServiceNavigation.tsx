import type { Language, Module } from "./types";

export type Selection = { module: string; section: string } | null;

export function ServiceNavigation({ language, modules, selection, onSelect }: { language: Language; modules: Module[]; selection: Selection; onSelect: (value: Selection) => void }) {
  const functional = modules
    .filter(module => module.service_kind === "FUNCTIONAL" && module.sections.length > 0)
    .sort((left, right) => (left.navigation_order ?? 0) - (right.navigation_order ?? 0) || left.module_id.localeCompare(right.module_id));
  return <aside className="service-navigation">
    <p className="nav-caption">{language === "ru" ? "Подключенные сервисы" : "Connected services"}</p>
    {functional.map(module => {
      if (module.sections.length === 1) {
        const item = module.sections[0];
        return <div className="module-nav" key={module.module_id}>
          <button aria-pressed={selection?.module === module.module_id && selection?.section === item.id} onClick={() => onSelect({ module: module.module_id, section: item.id })}>{module.title[language]}</button>
        </div>;
      }
      return <div className="module-nav" key={module.module_id}>
        <p>{module.title[language]}</p>
        {module.sections.map(item => <button key={item.id} aria-pressed={selection?.module === module.module_id && selection?.section === item.id} onClick={() => onSelect({ module: module.module_id, section: item.id })}>{item.title[language]}</button>)}
      </div>;
    })}
    {!functional.length && <p className="aelev-muted">{language === "ru" ? "Пока не подключены" : "Not connected yet"}</p>}
  </aside>;
}
