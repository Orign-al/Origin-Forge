"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Card, EmptyState, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../../components/PortalShell";
import { userDetail } from "../../../../lib/api";

const TABS = [
  "概览",
  "登录安全",
  "Linux 身份",
  "GPU 隔离",
  "Slurm",
  "容器",
  "配额",
  "SSH 公钥",
  "操作记录",
  "审计日志",
] as const;

function Value({ children }: { children: unknown }) {
  if (children === null || children === undefined || children === "")
    return <span className="muted">—</span>;
  return <>{String(children)}</>;
}

export default function UserDetailPage() {
  const params = useParams<{ id: string }>();
  const [activeTab, setActiveTab] = useState<(typeof TABS)[number]>("概览");
  const query = useQuery({
    queryKey: ["user", params.id],
    queryFn: () => userDetail(params.id),
  });
  if (query.isPending)
    return (
      <>
        <PageHeading title="用户详情" />
        <LoadingBlock />
      </>
    );
  if (query.isError)
    return (
      <>
        <PageHeading title="用户详情" />
        <ErrorBlock />
      </>
    );
  const user = query.data.user;
  const linux = user.linux_identity ?? {};
  const kv = (entries: Array<[string, unknown]>) => (
    <dl className="kv-grid">
      {entries.map(([label, value]) => (
        <div className="kv" key={label}>
          <dt>{label}</dt>
          <dd>
            <Value>{value}</Value>
          </dd>
        </div>
      ))}
    </dl>
  );
  return (
    <>
      <PageHeading
        title={user.display_name}
        description="网页身份、Linux 映射与计算资源状态严格分离"
        action={<StatusBadge value={user.account_state} />}
      />
      {user.normalized_login === "origin-al" ? (
        <div className="notice" style={{ marginBottom: 14 }}>
          当前 Origin-al 是平台恢复/管理账号，尚未进入受控 Pilot 计算身份流程。
        </div>
      ) : null}
      <div className="tabs" role="tablist" aria-label="用户详情分区">
        {TABS.map((tab) => (
          <button
            className={`tab ${activeTab === tab ? "tab-active" : ""}`}
            key={tab}
            onClick={() => setActiveTab(tab)}
            role="tab"
            aria-selected={activeTab === tab}
            type="button"
          >
            {tab}
          </button>
        ))}
      </div>
      <Card className="detail-panel">
        {activeTab === "概览"
          ? kv([
              ["网页登录名", user.login_name],
              ["规范化登录名", user.normalized_login],
              ["角色", user.roles.map((role) => role.name).join(", ")],
              ["账号状态", user.account_state],
              ["密码状态", user.password_state],
              ["计算 onboarding", user.resource_onboarding_state],
            ])
          : null}
        {activeTab === "登录安全" ? (
          <>
            {kv([
              ["密码状态", user.password_state],
              ["账号状态", user.account_state],
              ["激活时间", user.activated_at],
              ["最近登录", user.last_login_at],
            ])}
            {user.normalized_login === "origin-al" ? (
              <Link className="table-link" href="/account/security">
                打开当前账号安全设置
              </Link>
            ) : (
              <div className="notice">管理员会话撤销需要单独审批。</div>
            )}
          </>
        ) : null}
        {activeTab === "Linux 身份"
          ? kv([
              ["Linux 用户", linux.unix_username ?? user.unix_username],
              ["UID", linux.uid],
              ["GID", linux.gid],
              ["Shell", linux.shell],
              ["宿主访问", linux.host_access_state],
              ["资源状态", linux.onboarding_state],
            ])
          : null}
        {activeTab === "GPU 隔离"
          ? kv([
              ["隔离状态", linux.gpu_isolation_state ?? "NOT_APPLIED"],
              [
                "方法",
                linux.uid ? `user-${String(linux.uid)}.slice` : "NOT_APPLIED",
              ],
              ["宿主直接 GPU", linux.uid ? "DENIED" : "NOT_ENROLLED"],
            ])
          : null}
        {activeTab === "Slurm"
          ? kv([
              ["Account", linux.slurm_account],
              ["QOS", linux.slurm_qos],
              ["状态", user.resource_onboarding_state],
            ])
          : null}
        {activeTab === "容器"
          ? kv([
              ["容器", linux.container_name],
              ["SSH 端口", linux.container_port],
              ["GPU", linux.container_name ? "无" : "NOT_ENROLLED"],
            ])
          : null}
        {activeTab === "配额"
          ? kv([
              ["Project ID", linux.project_id],
              ["Quota bytes", linux.quota_bytes],
            ])
          : null}
        {activeTab === "SSH 公钥" ? (
          <EmptyState
            title="没有可展示的 SSH 公钥记录"
            detail="页面永不显示私钥或完整公钥正文"
          />
        ) : null}
        {activeTab === "操作记录" ? (
          <EmptyState
            title="请在审批任务页按用户筛选"
            detail="本页不伪造资源操作记录"
          />
        ) : null}
        {activeTab === "审计日志" ? (
          <EmptyState
            title="请在审计页按对象筛选"
            detail="审计不包含密码、token 或 session"
          />
        ) : null}
      </Card>
      <div className="section-grid">
        <SectionCard title="身份边界">
          网页密码与 Linux/SSH 密码完全分离；计算 onboarding
          不会因网页账号激活而自动执行。
        </SectionCard>
        <SectionCard title="当前阶段">
          所有资源写操作只形成审批任务和 Worker dry-run，Slurm 保持 DRAIN。
        </SectionCard>
      </div>
    </>
  );
}
