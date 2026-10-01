import type { Metadata } from "next";

import "./globals.css";

import { AppShell } from "@/components/AppShell";
import { ToastProvider } from "@/components/toast";

export const metadata: Metadata = {
  title: { default: "ViralClip AI", template: "%s · ViralClip AI" },
  description: "Vindt automatisch de momenten met het hoogste viral potential in lange YouTube-video's.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="nl" className="h-full antialiased">
      <body className="min-h-full">
        <ToastProvider>
          <AppShell>{children}</AppShell>
        </ToastProvider>
      </body>
    </html>
  );
}
