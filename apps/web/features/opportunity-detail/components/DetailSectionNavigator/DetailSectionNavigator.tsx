"use client";

import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import styles from "./DetailSectionNavigator.module.css";

type DetailTab = {
  id: string;
  label: string;
  count?: number;
};

export function DetailSectionNavigator({ tabs, children }: { tabs: DetailTab[]; children: ReactNode }) {
  const fallback = tabs[0]?.id ?? "decision";
  const [active, setActive] = useState(fallback);

  useEffect(() => {
    const selectFromHash = () => {
      const hash = window.location.hash.replace(/^#/, "");
      if (tabs.some((tab) => tab.id === hash)) setActive(hash);
    };
    selectFromHash();
    window.addEventListener("hashchange", selectFromHash);
    return () => window.removeEventListener("hashchange", selectFromHash);
  }, [tabs]);

  function select(id: string) {
    setActive(id);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${id}`);
  }

  return (
    <section className={styles.workspace}>
      <div className={styles.tabs} role="tablist" aria-label="Contenido de la oportunidad">
        {tabs.map((tab) => (
          <button
            aria-controls={`detail-pane-${tab.id}`}
            aria-selected={active === tab.id}
            className={active === tab.id ? styles.active : ""}
            id={`detail-tab-${tab.id}`}
            key={tab.id}
            onClick={() => select(tab.id)}
            role="tab"
            type="button"
          >
            {tab.label}
            {tab.count != null ? <span>{tab.count}</span> : null}
          </button>
        ))}
      </div>
      <div className={styles.content} data-active={active}>
        {children}
      </div>
    </section>
  );
}
