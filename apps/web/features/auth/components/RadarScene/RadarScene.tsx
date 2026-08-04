import styles from "./RadarScene.module.css";

const signals = [
  { x: 116, y: 104, size: 8, tone: "mint" },
  { x: 332, y: 84, size: 6, tone: "violet" },
  { x: 378, y: 238, size: 9, tone: "mint" },
  { x: 108, y: 278, size: 5, tone: "violet" },
];

export function RadarScene() {
  return (
    <div className={styles.scene} aria-hidden="true">
      <svg className={styles.radar} viewBox="0 0 480 360">
        <defs>
          <radialGradient id="bp-radar-glow">
            <stop offset="0" stopColor="#7568ff" stopOpacity=".32" />
            <stop offset=".58" stopColor="#6759ff" stopOpacity=".08" />
            <stop offset="1" stopColor="#6759ff" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="bp-radar-sweep" x1="0" y1="0" x2="1" y2="1">
            <stop stopColor="#8175ff" stopOpacity=".46" />
            <stop offset="1" stopColor="#8175ff" stopOpacity="0" />
          </linearGradient>
        </defs>
        <circle cx="240" cy="180" r="156" fill="url(#bp-radar-glow)" />
        <g className={styles.grid} fill="none">
          <circle cx="240" cy="180" r="132" />
          <circle cx="240" cy="180" r="94" />
          <circle cx="240" cy="180" r="56" />
          <path d="M240 48v264M108 180h264" />
          <path d="m147 87 186 186M333 87 147 273" opacity=".55" />
        </g>
        <g className={styles.sweep}>
          <path d="M240 180 240 54a126 126 0 0 1 108 61Z" fill="url(#bp-radar-sweep)" />
        </g>
        <g className={styles.orbit} fill="none" strokeLinecap="round">
          <circle className={styles.orbitOuter} cx="240" cy="180" pathLength="100" r="112" strokeDasharray="29 8 43 20" transform="rotate(-22 240 180)" />
          <circle className={styles.orbitMiddle} cx="240" cy="180" pathLength="100" r="78" strokeDasharray="25 14 31 30" transform="rotate(28 240 180)" />
          <circle className={styles.orbitInner} cx="240" cy="180" pathLength="100" r="48" strokeDasharray="19 18 27 36" transform="rotate(-44 240 180)" />
        </g>
        <circle cx="235" cy="184" r="24" fill="#6f61ff" />
        <circle cx="235" cy="184" r="40" fill="none" stroke="#8e84ff" strokeOpacity=".34" />
        {signals.map((signal, index) => (
          <g key={`${signal.x}-${signal.y}`} className={styles.signal} style={{ animationDelay: `${index * 480}ms` }}>
            <circle cx={signal.x} cy={signal.y} fill={signal.tone === "mint" ? "#6DDBAE" : "#A79FFF"} r={signal.size} />
            <circle className={styles.signalRing} cx={signal.x} cy={signal.y} fill="none" r={signal.size + 7} />
          </g>
        ))}
      </svg>
      <span className={`${styles.signalLabel} ${styles.signalOne}`}><i /> Alta afinidad</span>
      <span className={`${styles.signalLabel} ${styles.signalTwo}`}>81 · Tecnología</span>
      <span className={`${styles.signalLabel} ${styles.signalThree}`}>Cierra hoy</span>
    </div>
  );
}
