"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ACCESS_MODE_LABEL, navigation } from "@h100-portal/config";
import { Badge, Button } from "@h100-portal/ui";
import { alerts, logout, me, recordPageAccess } from "../lib/api";

const ROLE_LABELS: Record<string, string> = {
  platform_owner: "平台所有者",
  platform_admin: "平台管理员",
  operator: "运维操作员",
  auditor: "审计员",
  user: "用户",
};

const USER_NAVIGATION = [
  ["我的环境", "/"],
  ["连接", "/access"],
  ["网页终端", "/terminal"],
  ["作业", "/jobs"],
  ["开发容器", "/containers"],
  ["存储", "/storage"],
  ["SSH密钥", "/ssh-keys"],
  ["回收站", "/recycle-bin"],
  ["账号与安全", "/account/security"],
  ["帮助", "/help"],
] as const;

const UNPROVISIONED_USER_NAVIGATION = [
  ["我的环境", "/"],
  ["资源申请", "/compute-request"],
  ["SSH密钥", "/ssh-keys"],
  ["账号与安全", "/account/security"],
  ["帮助", "/help"],
] as const;

const STAGED_USER_NAVIGATION = [
  ["我的环境", "/"],
  ["SSH密钥", "/ssh-keys"],
  ["账号与安全", "/account/security"],
  ["帮助", "/help"],
] as const;

const UNPROVISIONED_ALLOWED_PATHS = new Set([
  "/",
  "/compute-request",
  "/ssh-keys",
  "/account/security",
  "/help",
  "/change-password",
]);

const STAGED_ALLOWED_PATHS = new Set([
  "/",
  "/ssh-keys",
  "/account/security",
  "/help",
  "/change-password",
]);

export function PortalShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });
  const alertQuery = useQuery({
    queryKey: ["shell-alerts"],
    queryFn: alerts,
    enabled: current.isSuccess && current.data?.role !== "user",
    retry: false,
    refetchInterval: 30_000,
  });
  useEffect(() => {
    if (current.isError) router.replace("/login");
  }, [current.isError, router]);
  useEffect(() => {
    if (
      current.data?.user.password_state === "RESET_REQUIRED" &&
      pathname !== "/change-password"
    ) {
      router.replace("/change-password");
    }
  }, [current.data?.user.password_state, pathname, router]);
  useEffect(() => {
    if (!current.isSuccess || current.data.role !== "user") return;
    const state = current.data.user.resource_onboarding_state;
    const allowed =
      state === "NOT_ENROLLED"
        ? UNPROVISIONED_ALLOWED_PATHS
        : state === "STAGED"
          ? STAGED_ALLOWED_PATHS
          : null;
    const protectedResourceProbe = pathname.startsWith("/compute-requests/");
    if (allowed && !allowed.has(pathname) && !protectedResourceProbe) {
      router.replace("/");
    }
  }, [current.data, current.isSuccess, pathname, router]);
  useEffect(() => {
    if (
      current.isSuccess &&
      current.data.user.password_state !== "RESET_REQUIRED"
    ) {
      void recordPageAccess(pathname).catch(() => undefined);
    }
  }, [current.data?.user.password_state, current.isSuccess, pathname]);
  if (current.isPending)
    return (
      <div className="auth-page">
        <div className="muted">正在验证会话…</div>
      </div>
    );
  if (current.isError) {
    return (
      <div className="auth-page">
        <div className="muted">正在跳转登录页…</div>
      </div>
    );
  }
  const user = current.data.user;
  const role = current.data.role;
  const activeNavigation =
    role === "user"
      ? user.resource_onboarding_state === "NOT_ENROLLED"
        ? UNPROVISIONED_USER_NAVIGATION
        : user.resource_onboarding_state === "STAGED"
          ? STAGED_USER_NAVIGATION
          : USER_NAVIGATION
      : navigation;
  const alertData = alertQuery.data as { count?: unknown } | undefined;
  const alertCount =
    typeof alertData?.count === "number" ? alertData.count : "—";
  const searchResults = search.trim()
    ? activeNavigation.filter(([label]) =>
        label.toLowerCase().includes(search.trim().toLowerCase()),
      )
    : [];
  async function signOut() {
    await logout().catch(() => undefined);
    queryClient.clear();
    router.replace("/login");
  }
  return (
    <div className="portal-frame">
      <aside className="portal-sidebar">
        <div className="brand">
          <div className="brand-title">H100 管理平台</div>
          <div className="brand-subtitle">
            {role === "user"
              ? "个人计算环境 · Portal-5A-1B"
              : "单机控制面 · Portal-5A-1B"}
          </div>
        </div>
        <nav className="nav-group" aria-label="主导航">
          <div className="nav-label">
            {role === "user" ? "我的资源" : "平台"}
          </div>
          {activeNavigation.map(([label, href]) => (
            <Link
              key={href}
              href={href}
              className={`nav-link ${pathname === href ? "nav-link-active" : ""}`}
            >
              {label}
            </Link>
          ))}
        </nav>
        <div className="sidebar-foot">
          {role === "user" ? (
            <>
              {user.resource_onboarding_state === "NOT_ENROLLED"
                ? "计算资源未配置"
                : user.resource_onboarding_state === "STAGED"
                  ? "等待 Container SSH 密钥"
                  : "Host SSH 已禁用"}
              <br />
              {user.resource_onboarding_state === "NOT_ENROLLED"
                ? "Portal Identity Only"
                : user.resource_onboarding_state === "STAGED"
                  ? "Container STOPPED · Lease 未开始"
                  : "GPU 上限 1 · 租约受控"}
            </>
          ) : (
            <>
              Production Pilot ACTIVE
              <br />
              单节点 · 单受管用户
            </>
          )}
        </div>
      </aside>
      <div className="portal-main">
        <header className="topbar">
          <div className="topbar-left">
            <span className="env-label">H100 单节点环境</span>
            <span className="local-mode">{ACCESS_MODE_LABEL}</span>
          </div>
          <div className="topbar-search">
            <label htmlFor="global-search" className="sr-only">
              全局模块搜索
            </label>
            <input
              id="global-search"
              className="topbar-search-input"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索模块"
              autoComplete="off"
            />
            {searchResults.length ? (
              <div className="topbar-search-results">
                {searchResults.map(([label, href]) => (
                  <Link key={href} href={href} onClick={() => setSearch("")}>
                    {label}
                  </Link>
                ))}
              </div>
            ) : null}
          </div>
          <div className="topbar-right">
            {role !== "user" ? (
              <span className="muted">节点：sagsh100server</span>
            ) : null}
            {role !== "user" ? (
              <span
                className="alert-count"
                aria-label={`当前告警 ${alertCount} 个`}
              >
                告警 {alertCount}
              </span>
            ) : null}
            <Link className="user-chip" href="/account/security">
              <span className="avatar">O</span>
              {user.display_name}
              <span className="muted">{ROLE_LABELS[role] ?? role}</span>
            </Link>
            <Button onClick={signOut}>退出</Button>
          </div>
        </header>
        <main className="content">
          {current.data.ssh_enrollment?.required &&
          current.data.ssh_enrollment.setup_path ? (
            <div className="onboarding-banner" role="status">
              <div>
                <strong>
                  完成 Container SSH 密钥设置后即可申请激活计算环境。
                </strong>
                <span>Host SSH、容器访问与 Lease 仍保持关闭。</span>
              </div>
              <Link
                className="ui-button ui-button-primary"
                href={current.data.ssh_enrollment.setup_path}
              >
                设置 SSH 密钥
              </Link>
            </div>
          ) : null}
          {children}
        </main>
      </div>
    </div>
  );
}

export function PageHeading({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <div className="breadcrumb">平台 / {title}</div>
        <h1>{title}</h1>
        {description ? (
          <div className="page-description">{description}</div>
        ) : null}
      </div>
      {action ? <div className="heading-actions">{action}</div> : null}
    </div>
  );
}

export function LoadingBlock() {
  return (
    <div className="ui-card">
      <div className="ui-empty">正在读取平台数据…</div>
    </div>
  );
}
export function ErrorBlock({
  message = "数据源不可用，未将其显示为正常或 0。",
}: {
  message?: string;
}) {
  return <div className="error-box">{message}</div>;
}
export function UnauthorizedBlock() {
  return <div className="error-box">当前账号无权查看此模块。</div>;
}
export function StaleNotice() {
  return (
    <div className="notice">
      数据来自最近一次成功采样；当前 Worker 或数据源可能暂时不可用。
    </div>
  );
}

export function SectionCard({
  title,
  subtitle,
  children,
  action,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="ui-card section-card">
      <div className="section-card-header">
        <div>
          <div className="section-card-title">{title}</div>
          {subtitle ? (
            <div className="section-card-subtitle">{subtitle}</div>
          ) : null}
        </div>
        {action}
      </div>
      <div className="section-card-body">{children}</div>
    </section>
  );
}

export function RoleBadge({ role }: { role: string }) {
  return (
    <Badge label={role} tone={role === "platform_owner" ? "info" : "neutral"} />
  );
}
