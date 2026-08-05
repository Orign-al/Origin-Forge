"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button, Card, Input, StatusBadge } from "@h100-portal/ui";
import {
  PageHeading,
  LoadingBlock,
  ErrorBlock,
  SectionCard,
  RoleBadge,
} from "../../../components/PortalShell";
import {
  ObjectTable,
  type SimpleColumnDef,
  type SimpleRow,
} from "../../../components/Tables";
import { ApiError, users } from "../../../lib/api";

const USER_COLUMNS: SimpleColumnDef[] = [
  {
    accessorKey: "login_name",
    header: "网页登录名",
    cell: (info) => (
      <Link
        className="table-link"
        href={`/users/${String(info.row.original.id)}`}
      >
        {String(info.getValue())}
      </Link>
    ),
  },
  {
    accessorKey: "role",
    header: "角色",
    cell: (info) => <RoleBadge role={String(info.getValue())} />,
  },
  {
    accessorKey: "account_state",
    header: "账号状态",
    cell: (info) => <StatusBadge value={String(info.getValue())} />,
  },
  { accessorKey: "unix", header: "Linux 映射" },
  {
    accessorKey: "onboarding",
    header: "计算 onboarding",
    cell: (info) => <StatusBadge value={String(info.getValue())} />,
  },
  {
    accessorKey: "gpu",
    header: "GPU 隔离",
    cell: (info) => <StatusBadge value={String(info.getValue())} />,
  },
];

export default function UsersPage() {
  const query = useQuery({ queryKey: ["users"], queryFn: users });
  const [filter, setFilter] = useState("");
  if (query.isPending)
    return (
      <>
        <PageHeading
          title="用户"
          description="网页身份与 Linux 资源身份分开管理"
        />
        <LoadingBlock />
      </>
    );
  if (
    query.isError &&
    query.error instanceof ApiError &&
    query.error.status === 403
  )
    return (
      <>
        <PageHeading
          title="用户"
          description="网页身份与 Linux 资源身份分开管理"
        />
        <div className="error-box">当前账号无权查看此模块。</div>
      </>
    );
  if (query.isError)
    return (
      <>
        <PageHeading
          title="用户"
          description="网页身份与 Linux 资源身份分开管理"
        />
        <ErrorBlock />
      </>
    );
  const rows = query.data.users
    .filter((user) =>
      `${user.login_name} ${user.unix_username ?? ""} ${user.account_state}`
        .toLowerCase()
        .includes(filter.toLowerCase()),
    )
    .map((user) => ({
      ...user,
      id: user.id,
      role: user.roles.map((role) => role.name).join(", "),
      onboarding: user.resource_onboarding_state,
      gpu: String(user.linux_identity?.gpu_isolation_state ?? "NOT_APPLIED"),
      unix: user.unix_username ?? "—",
    }));
  const origin = query.data.users.find(
    (user) => user.normalized_login === "origin-al",
  );
  return (
    <>
      <PageHeading
        title="用户"
        description="网页平台账号、Linux 映射和受控计算身份"
        action={
          <Button tone="primary" disabled>
            创建用户（后续审批）
          </Button>
        }
      />
      {origin ? (
        <div className="notice" style={{ marginBottom: 14 }}>
          Origin-al 是平台恢复/管理账号：网页身份可用后，计算身份仍为{" "}
          <strong>{origin.resource_onboarding_state}</strong>，不会自动进入
          Pilot。
        </div>
      ) : null}
      <Card style={{ padding: 14 }}>
        <div className="filter-row">
          <Input
            aria-label="筛选用户"
            placeholder="筛选登录名、Linux 用户或状态"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <span className="muted">
            {rows.length} / {query.data.count} 个账号
          </span>
        </div>
        <ObjectTable rows={rows} columns={USER_COLUMNS} />
      </Card>
      <div className="section-grid">
        <SectionCard title="状态说明">
          <dl className="kv-grid">
            <div className="kv">
              <dt>网页账号</dt>
              <dd>INVITED / ACTIVE / SUSPENDED</dd>
            </div>
            <div className="kv">
              <dt>资源 onboarding</dt>
              <dd>NOT_ENROLLED / STAGED / ACTIVE</dd>
            </div>
            <div className="kv">
              <dt>高权限组</dt>
              <dd>不由 Portal 自动添加</dd>
            </div>
            <div className="kv">
              <dt>GPU 策略</dt>
              <dd>仅精确 UID slice</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="安全边界">
          <div className="notice">
            本阶段不创建 Linux 用户、不安装 SSH
            key、不启动员工容器；所有写操作先形成审批任务。
          </div>
        </SectionCard>
      </div>
    </>
  );
}
