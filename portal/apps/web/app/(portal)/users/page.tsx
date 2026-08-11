"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button, Card, Input, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  RoleBadge,
  SectionCard,
} from "../../../components/PortalShell";
import { ObjectTable, type SimpleColumnDef } from "../../../components/Tables";
import { ApiError, createPortalUser, users } from "../../../lib/api";

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

const USER_COLUMNS: SimpleColumnDef[] = [
  {
    accessorKey: "login_name",
    header: "用户",
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
    header: "Portal状态",
    cell: (info) => <StatusBadge value={String(info.getValue())} />,
  },
  { accessorKey: "password", header: "密码状态" },
  { accessorKey: "compute", header: "计算环境" },
  { accessorKey: "lease", header: "Lease" },
  { accessorKey: "last_login", header: "最后登录" },
  { accessorKey: "created", header: "创建时间" },
];

export default function UsersPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["users"], queryFn: users });
  const [filter, setFilter] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [loginName, setLoginName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("user");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    if (!loginName.trim() || !displayName.trim()) {
      setMessage("请输入登录名和显示名称。");
      return;
    }
    setBusy(true);
    try {
      const result = await createPortalUser({
        login_name: loginName,
        display_name: displayName,
        role,
        note: note.trim() || null,
      });
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      router.push(`/users/${result.user.id}`);
    } catch (reason) {
      setMessage(
        reason instanceof ApiError
          ? reason.message
          : "用户创建失败，未创建任何资源。",
      );
    } finally {
      setBusy(false);
    }
  }

  if (query.isPending)
    return (
      <>
        <PageHeading title="用户" description="Portal 身份与计算资源严格分离" />
        <LoadingBlock />
      </>
    );
  if (query.isError)
    return (
      <>
        <PageHeading title="用户" description="Portal 身份与计算资源严格分离" />
        {query.error instanceof ApiError && query.error.status === 403 ? (
          <div className="error-box">当前账号无权查看此模块。</div>
        ) : (
          <ErrorBlock />
        )}
      </>
    );

  const rows = query.data.users
    .filter((user) =>
      `${user.login_name} ${user.display_name} ${user.account_state}`
        .toLowerCase()
        .includes(filter.toLowerCase()),
    )
    .map((user) => ({
      ...user,
      role: user.roles.map((item) => item.name).join(", "),
      password:
        user.password_state === "SETUP_REQUIRED"
          ? "NOT SET"
          : user.password_state,
      compute:
        user.resource_onboarding_state === "NOT_ENROLLED"
          ? "NOT PROVISIONED"
          : user.resource_onboarding_state,
      lease:
        user.resource_onboarding_state === "NOT_ENROLLED" ? "—" : "查看详情",
      last_login: localTime(user.last_login_at),
      created: localTime(user.created_at),
    }));

  return (
    <>
      <PageHeading
        title="用户"
        description="创建 Portal 账号；Linux、Container、Slurm、Quota 与 Lease 由后续流程配置"
        action={
          <Button
            tone="primary"
            onClick={() => setShowCreate((value) => !value)}
          >
            {showCreate ? "取消新建" : "新建用户"}
          </Button>
        }
      />
      {showCreate ? (
        <Card className="identity-create-panel">
          <div className="detail-section-heading">
            <div>
              <h2>新建 Portal 用户</h2>
              <p className="muted">
                初始状态为 INVITED，密码由用户通过一次性链接设置。
              </p>
            </div>
            <StatusBadge value="IDENTITY ONLY" />
          </div>
          <form onSubmit={submit}>
            <div className="identity-form-grid">
              <div className="form-field">
                <label htmlFor="login-name">Username / Login Name</label>
                <Input
                  id="login-name"
                  value={loginName}
                  onChange={(event) => setLoginName(event.target.value)}
                  placeholder="origin-pilot2"
                  autoComplete="off"
                />
              </div>
              <div className="form-field">
                <label htmlFor="display-name">Display Name</label>
                <Input
                  id="display-name"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="Origin Pilot 2"
                  autoComplete="off"
                />
              </div>
              <div className="form-field">
                <label htmlFor="role">Role</label>
                <select
                  id="role"
                  className="ui-input"
                  value={role}
                  onChange={(event) => setRole(event.target.value)}
                >
                  <option value="user">user（默认）</option>
                  <option value="operator">operator</option>
                  <option value="auditor">auditor</option>
                </select>
              </div>
              <div className="form-field identity-note-field">
                <label htmlFor="note">备注（可选）</label>
                <textarea
                  id="note"
                  className="ui-input identity-note"
                  value={note}
                  maxLength={500}
                  onChange={(event) => setNote(event.target.value)}
                />
              </div>
            </div>
            {message ? (
              <div className="error-box" role="alert">
                {message}
              </div>
            ) : null}
            <div className="form-actions">
              <Button tone="primary" type="submit" disabled={busy}>
                {busy ? "创建中…" : "创建邀请账号"}
              </Button>
            </div>
          </form>
        </Card>
      ) : null}
      <Card style={{ padding: 14 }}>
        <div className="filter-row">
          <Input
            aria-label="筛选用户"
            placeholder="筛选登录名、显示名称或状态"
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
        <SectionCard title="邀请流程">
          ADMIN CREATE USER → INVITED → ONE-TIME SETUP LINK → USER SETS PASSWORD
          → ACTIVE
        </SectionCard>
        <SectionCard title="资源边界">
          新建 Portal 用户不会创建 Linux 用户、Container、Slurm
          Association、Quota、GPU Policy 或 Lease。
        </SectionCard>
      </div>
    </>
  );
}
