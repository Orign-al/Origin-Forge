import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  I18nProvider,
  isLocale,
  LanguageSwitcher,
  LOCALE_COOKIE,
  useI18n,
} from "../lib/i18n";
import { PageHeading } from "../components/PortalShell";

function Probe() {
  const { locale, t } = useI18n();
  return (
    <>
      <LanguageSwitcher />
      <output>{locale}</output>
      <span>{t("登录")}</span>
      <PageHeading title="我的环境" description="自己的长期开发环境" />
    </>
  );
}

describe("Portal language preference", () => {
  it("switches the interface immediately and persists a constrained cookie", () => {
    render(
      <I18nProvider initialLocale="zh-CN">
        <Probe />
      </I18nProvider>,
    );

    expect(screen.getByText("登录")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "切换界面语言" }), {
      target: { value: "en-US" },
    });

    expect(screen.getByText("Sign in")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "My Environment" }),
    ).toBeInTheDocument();
    expect(screen.getByText("en-US")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en-US");
    expect(document.cookie).toContain(`${LOCALE_COOKIE}=en-US`);
  });

  it("accepts only supported locale values", () => {
    expect(isLocale("zh-CN")).toBe(true);
    expect(isLocale("en-US")).toBe(true);
    expect(isLocale("en-GB")).toBe(false);
    expect(isLocale("<script>")).toBe(false);
    expect(isLocale(undefined)).toBe(false);
  });
});
