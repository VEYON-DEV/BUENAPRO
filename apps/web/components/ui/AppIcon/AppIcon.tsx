import styles from "./AppIcon.module.css";

const paths = {
  home: <path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-4.5v-6h-5v6H5a1 1 0 0 1-1-1v-9.5Z" />,
  inbox: <path d="M4 7h16v10.5A2.5 2.5 0 0 1 17.5 20h-11A2.5 2.5 0 0 1 4 17.5V7Zm0 0 6.9 5.2a1.8 1.8 0 0 0 2.2 0L20 7" />,
  search: <path d="M10.8 18.1a7.3 7.3 0 1 1 5.2-2.1l4 4M7.5 10.8a3.3 3.3 0 0 1 3.3-3.3" />,
  track: <path d="M5 6.5h8.5a4 4 0 0 1 0 8H9m0 0 3-3m-3 3 3 3M5 18.5h3" />,
  profile: <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm7 8a7 7 0 0 0-14 0" />,
  settings: <path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm0-12v2m0 13v2m8.5-8.5h-2m-13 0h-2m14.5-6.5-1.4 1.4M6.9 17.1l-1.4 1.4m0-13 1.4 1.4m10.2 10.2 1.4 1.4" />,
  admin: <path d="M5 7.5 12 4l7 3.5V12c0 4.2-2.7 7.2-7 8-4.3-.8-7-3.8-7-8V7.5Zm4.2 4.4 2 2 3.8-4" />,
  bell: <path d="M7 17h10l-1.2-1.7V11a3.8 3.8 0 0 0-7.6 0v4.3L7 17Zm3.2 3h3.6" />,
  doc: <path d="M7 3.5h6.5L18 8v12.5H7V3.5Zm6.5 0V8H18M9.5 12h5M9.5 15h5" />,
  check: <path d="m5.5 12.5 4 4 9-9" />,
  arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
  filter: <path d="M5 7h14M8 12h8m-5 5h2" />,
  close: <path d="m7 7 10 10M17 7 7 17" />,
  mark: <path d="M7 3.5h7l3.5 3.5v13.5H7V3.5Zm7 0V7h3.5M9.5 13l2.2 2.2 4.8-5" />,
  building: <path d="M5 20V5.5A1.5 1.5 0 0 1 6.5 4h6A1.5 1.5 0 0 1 14 5.5V20M5 20h15M14 9h3.5a1.5 1.5 0 0 1 1.5 1.5V20M8 8h3M8 12h3M8 16h3" />,
  clock: <path d="M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17Zm0-13.5v5l3 2" />,
  pin: <path d="M12 21c-4.3-3.6-6.5-7.1-6.5-10.3a6.5 6.5 0 0 1 13 0C18.5 13.9 16.3 17.4 12 21Zm0-8.2a2.3 2.3 0 1 0 0-4.6 2.3 2.3 0 0 0 0 4.6Z" />,
  alert: <path d="M12 4.5 20.5 19h-17L12 4.5Zm0 5.5v4m0 2.6v.4" />,
  tag: <path d="m4 11.5 7.5-7.5H20v8.5L12.5 20 4 11.5Zm12-3.5h.01" />,
  bookmark: <path d="M6.5 4h11v16L12 16.6 6.5 20V4Z" />,
};

export type AppIconName = keyof typeof paths;

export function AppIcon({ name }: { name: AppIconName }) {
  return (
    <svg className={styles.icon} aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
      {paths[name]}
    </svg>
  );
}
