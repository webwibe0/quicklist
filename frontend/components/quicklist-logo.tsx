import { useId } from "react";

/** The approved checklist mark; public/quicklist-mark.svg is its favicon edition. */
export function QuickListLogo() {
  const id = useId().replaceAll(':','');
  return <svg data-quicklist-logo viewBox="0 0 64 64" fill="none" aria-hidden="true">
    <defs>
      <linearGradient id={`${id}-base`} x1="8" y1="4" x2="54" y2="64" gradientUnits="userSpaceOnUse"><stop stopColor="#759365"/><stop offset=".46" stopColor="#254b2b"/><stop offset="1" stopColor="#09271c"/></linearGradient>
      <linearGradient id={`${id}-edge`} x1="6" y1="3" x2="57" y2="62" gradientUnits="userSpaceOnUse"><stop stopColor="#ecf0d5"/><stop offset=".5" stopColor="#a4b783"/><stop offset="1" stopColor="#446244"/></linearGradient>
    </defs>
    <rect x="2" y="2" width="60" height="60" rx="18" fill={`url(#${id}-base)`} stroke={`url(#${id}-edge)`} strokeWidth="2"/>
    <path d="M8 21C8 12 12 8 21 8H44" stroke="#fff" strokeOpacity=".14" strokeWidth="2" strokeLinecap="round"/>
    <g stroke="#f4f4dc" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="m16 21 3 3 5-6M32 21h15M16 33l3 3 5-6M32 33h15M32 45h15"/>
    </g>
    <circle cx="19" cy="45" r="2.1" fill="#f4f4dc"/>
  </svg>;
}
