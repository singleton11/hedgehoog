// Small, decorative UI icons. Accessible names belong to their surrounding controls.
export default function Icon({ name, className = '' }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="2" /><rect x="14" y="3" width="7" height="7" rx="2" /><rect x="3" y="14" width="7" height="7" rx="2" /><rect x="14" y="14" width="7" height="7" rx="2" /></>,
    users: <><circle cx="9" cy="7" r="4" /><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
    chart: <><path d="M4 4v16h16M8 14l4-4 4 2 5-7" /></>,
    layers: <><path d="m12 3 10 5-10 5L2 8l10-5ZM3 12l9 5 9-5M3 16l9 5 9-5" /></>,
    arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
    logout: <><path d="M9 4H4v16h5M10 12h11m-4-4 4 4-4 4" /></>,
    shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z" /><path d="m8 12 3 3 5-6" /></>,
  };
  return <svg className={`icon ${className}`} width="20" height="20" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    {paths[name]}
  </svg>;
}

export function BrandMark() {
  return <span className="brand-mark" aria-hidden="true">
    <svg width="25" height="25" viewBox="0 0 28 28" fill="none" focusable="false">
      <path d="M5 21V14M13 21V6M21 21V10" stroke="currentColor" strokeWidth="5" strokeLinecap="round" />
    </svg>
  </span>;
}
