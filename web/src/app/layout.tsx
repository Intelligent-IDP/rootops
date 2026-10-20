import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/sidebar";

export const metadata: Metadata = {
  title: "RootOps",
  description: "AI-Native Developer Platform",
  icons: { icon: "/favicon.ico" },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <head>
        <link
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Manrope:wght@400;500;600;700;800&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="bg-bg text-text min-h-screen">
        <div className="flex flex-col lg:flex-row min-h-screen">
          <Sidebar />
          <main className="min-w-0 flex-1 px-4 py-6 sm:px-6 sm:py-8 xl:px-10 xl:py-9">
            <div className="mx-auto w-full max-w-[1480px]">
            {children}
            </div>
          </main>
        </div>
      </body>
    </html>
  );
}
