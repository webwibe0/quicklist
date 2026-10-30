import type { Metadata } from "next";
import "./globals.css";
import "./approved-ui.css";
import "./planner.css";
import "./public-pages.css";

export const metadata: Metadata = {
  applicationName: "QuickList",
  title: "QuickList — личный ассистент",
  description: "Меньше держать в голове. Задачи, напоминания и встречи — обычными словами.",
  icons: {icon: '/quicklist-mark.svg'},
};

export default function RootLayout({children}: {children: React.ReactNode}) {
  return <html lang="ru"><body>{children}</body></html>;
}
