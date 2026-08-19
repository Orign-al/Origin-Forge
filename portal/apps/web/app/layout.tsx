import type { Metadata } from "next";
import { PORTAL_TITLE } from "@h100-portal/config";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: PORTAL_TITLE,
  description: "Origin Forge H100 多用户 GPU 计算平台",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
