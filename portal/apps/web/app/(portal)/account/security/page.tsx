"use client";

import { type FormEvent, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button, Card, Input, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
} from "../../../../components/PortalShell";
import {
  changePassword,
  me,
  revokeOtherSessions,
  revokeSession,
  sessions,
} from "../../../../lib/api";

export default function AccountSecurityPage() {
  const queryClient = useQueryClient();
  const current = useQuery({ queryKey: ["me"], queryFn: me });
  const sessionQuery = useQuery({ queryKey: ["sessions"], queryFn: sessions });
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (current.isPending || sessionQuery.isPending)
    return (
      <>
        <PageHeading title="账号安全" />
        <LoadingBlock />
      </>
    );
  if (current.isError || sessionQuery.isError)
    return (
      <>
        <PageHeading title="账号安全" />
        <ErrorBlock />
      </>
    );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    if (newPassword.length < 14 || newPassword !== confirmation) {
      setMessage("新密码至少 14 个字符，且两次输入必须一致。");
      return;
    }
    setBusy(true);
    try {
      await changePassword({
        current_password: currentPassword,
        new_password: newPassword,
        confirmation,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmation("");
      setMessage("网页密码已修改，其他会话已撤销，当前会话已旋转。");
      await queryClient.invalidateQueries({ queryKey: ["sessions"] });
    } catch {
      setMessage("密码修改失败；未修改 Linux 或 SSH 密码。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeading
        title="账号安全"
        description="只管理 Portal 密码与服务端会话"
      />
      <div className="section-grid">
        <Card className="detail-panel">
          <h2>当前网页身份</h2>
          <dl className="kv-grid">
            <div className="kv">
              <dt>登录名</dt>
              <dd>{current.data.user.login_name}</dd>
            </div>
            <div className="kv">
              <dt>角色</dt>
              <dd>{current.data.role}</dd>
            </div>
            <div className="kv">
              <dt>账号</dt>
              <dd>
                <StatusBadge value={current.data.user.account_state} />
              </dd>
            </div>
            <div className="kv">
              <dt>计算身份</dt>
              <dd>
                <StatusBadge
                  value={current.data.user.resource_onboarding_state}
                />
              </dd>
            </div>
          </dl>
          <div className="notice">
            网页密码与 Linux shadow、SSH 密码和 authorized_keys 完全分离。
          </div>
        </Card>
        <Card className="detail-panel">
          <h2>修改网页密码</h2>
          <form onSubmit={submit}>
            <div className="form-field">
              <label htmlFor="current-password">当前网页密码</label>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
            </div>
            <div className="form-field">
              <label htmlFor="new-password">新网页密码</label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
              />
            </div>
            <div className="form-field">
              <label htmlFor="confirm-password">再次输入</label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </div>
            <Button tone="primary" type="submit" disabled={busy}>
              保存网页密码
            </Button>
            {message ? (
              <div className="notice" role="status">
                {message}
              </div>
            ) : null}
          </form>
        </Card>
      </div>
      <Card className="detail-panel">
        <div className="section-card-header">
          <div>
            <h2>活动会话</h2>
            <div className="muted">空闲 30 分钟，绝对 12 小时</div>
          </div>
          <Button
            onClick={async () => {
              const result = await revokeOtherSessions();
              setMessage(`已撤销 ${result.revoked} 个其他会话。`);
              await queryClient.invalidateQueries({ queryKey: ["sessions"] });
            }}
          >
            撤销其他会话
          </Button>
        </div>
        <div className="ui-table-wrap">
          <table className="ui-table">
            <thead>
              <tr>
                <th>创建时间</th>
                <th>最近活动</th>
                <th>来源</th>
                <th>绝对到期</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {sessionQuery.data.map((session) => (
                <tr key={session.id}>
                  <td>{session.created_at}</td>
                  <td>{session.last_seen_at}</td>
                  <td>{session.source_ip}</td>
                  <td>{session.absolute_expires_at}</td>
                  <td>
                    <StatusBadge
                      value={session.current ? "CURRENT" : "ACTIVE"}
                    />
                  </td>
                  <td>
                    <Button
                      disabled={session.current}
                      onClick={async () => {
                        await revokeSession(session.id);
                        await queryClient.invalidateQueries({
                          queryKey: ["sessions"],
                        });
                      }}
                    >
                      撤销
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}
