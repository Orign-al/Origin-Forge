import type { Metadata } from "next";
import { cookies } from "next/headers";
import { PORTAL_TITLE } from "@h100-portal/config";
import "./globals.css";
import { Providers } from "./providers";
import { isLocale, LOCALE_COOKIE } from "../lib/i18n-config";

export const metadata: Metadata = {
  title: PORTAL_TITLE,
  description: "Origin Forge H100 多用户 GPU 计算平台",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const preferredLocale = (await cookies()).get(LOCALE_COOKIE)?.value;
  const locale = isLocale(preferredLocale) ? preferredLocale : "zh-CN";
  return (
    <html lang={locale}>
      <body>
        <Providers initialLocale={locale}>{children}</Providers>
      </body>
    </html>
  );
}
