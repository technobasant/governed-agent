import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "governed-agent",
  description: "Review what an AI agent found and proposed before any of it becomes state.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip" href="#main">
          Skip to content
        </a>
        <header className="top">
          <Link href="/" className="brand">
            governed-agent
          </Link>
          <nav aria-label="Main">
            <Link href="/">Runs</Link>
            <Link href="/review">Review queue</Link>
            <Link href="/audit">Audit log</Link>
          </nav>
        </header>
        <main id="main">{children}</main>
      </body>
    </html>
  );
}
