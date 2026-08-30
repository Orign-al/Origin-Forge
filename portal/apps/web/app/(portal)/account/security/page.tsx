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
  cliTokens,
  createCliToken,
  me,
  reauthenticate,
  revokeCliToken,
  revokeOtherSessions,
  revokeSession,
  sessions,
} from "../../../../lib/api";
import { useI18n } from "../../../../lib/i18n";
import { copyText } from "../../../../lib/ssh-key";

export default function AccountSecurityPage() {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const current = useQuery({ queryKey: ["me"], queryFn: me });
  const sessionQuery = useQuery({ queryKey: ["sessions"], queryFn: sessions });
  const tokenQuery = useQuery({
    queryKey: ["cli-tokens"],
    queryFn: cliTokens,
    enabled: current.data?.role === "user",
  });
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tokenLabel, setTokenLabel] = useState("");
  const [tokenPassword, setTokenPassword] = useState("");
  const [tokenExpiry, setTokenExpiry] = useState("90");
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [tokenMessage, setTokenMessage] = useState<string | null>(null);
  const [tokenBusy, setTokenBusy] = useState(false);
  const ordinaryUser = current.data?.role === "user";
  if (
    current.isPending ||
    sessionQuery.isPending ||
    (ordinaryUser && tokenQuery.isPending)
  )
    return (
      <>
        <PageHeading title="账号安全" />
        <LoadingBlock />
      </>
    );
  if (
    current.isError ||
    sessionQuery.isError ||
    (ordinaryUser && tokenQuery.isError)
  )
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
      setMessage(t("新密码至少 14 个字符，且两次输入必须一致。"));
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
      setMessage(t("网页密码已修改，其他会话已撤销，当前会话已旋转。"));
      await queryClient.invalidateQueries({ queryKey: ["sessions"] });
    } catch {
      setMessage(t("密码修改失败；未修改 Linux 或 SSH 密码。"));
    } finally {
      setBusy(false);
    }
  }

  async function submitCliToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTokenMessage(null);
    setCreatedToken(null);
    if (!tokenLabel.trim() || !tokenPassword) {
      setTokenMessage(t("请输入Token名称和当前网页密码。"));
      return;
    }
    setTokenBusy(true);
    try {
      await reauthenticate(tokenPassword);
      const created = await createCliToken({
        label: tokenLabel.trim(),
        expires_in_days: tokenExpiry === "never" ? null : Number(tokenExpiry),
      });
      setCreatedToken(created.token);
      setTokenLabel("");
      setTokenPassword("");
      setTokenMessage(t("CLI Token已创建；关闭本页后不会再次显示明文。"));
      await queryClient.invalidateQueries({ queryKey: ["cli-tokens"] });
    } catch {
      setTokenMessage(t("CLI Token创建失败；没有生成或保存新的凭据。"));
    } finally {
      setTokenBusy(false);
    }
  }

  return (
    <>
      <PageHeading
        title="账号安全"
        description="只管理 Portal 密码、服务端会话与 CLI Token"
      />
      <div className="section-grid">
        <Card className="detail-panel">
          <h2>{t("当前网页身份")}</h2>
          <dl className="kv-grid">
            <div className="kv">
              <dt>{t("登录名")}</dt>
              <dd>{current.data.user.login_name}</dd>
            </div>
            <div className="kv">
              <dt>{t("角色")}</dt>
              <dd>{current.data.role}</dd>
            </div>
            <div className="kv">
              <dt>{t("账号")}</dt>
              <dd>
                <StatusBadge value={current.data.user.account_state} />
              </dd>
            </div>
            <div className="kv">
              <dt>{t("计算身份")}</dt>
              <dd>
                <StatusBadge
                  value={current.data.user.resource_onboarding_state}
                />
              </dd>
            </div>
          </dl>
          <div className="notice">
            {t(
              "网页密码与 Linux shadow、SSH 密码和 authorized_keys 完全分离。",
            )}
          </div>
        </Card>
        <Card className="detail-panel">
          <h2>{t("修改网页密码")}</h2>
          <form onSubmit={submit}>
            <div className="form-field">
              <label htmlFor="current-password">{t("当前网页密码")}</label>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
            </div>
            <div className="form-field">
              <label htmlFor="new-password">{t("新网页密码")}</label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
              />
            </div>
            <div className="form-field">
              <label htmlFor="confirm-password">{t("再次输入")}</label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </div>
            <Button tone="primary" type="submit" disabled={busy}>
              {t("保存网页密码")}
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
            <h2>{t("活动会话")}</h2>
            <div className="muted">{t("空闲 30 分钟，绝对 12 小时")}</div>
          </div>
          <Button
            onClick={async () => {
              const result = await revokeOtherSessions();
              setMessage(
                t("已撤销 {count} 个其他会话。", { count: result.revoked }),
              );
              await queryClient.invalidateQueries({ queryKey: ["sessions"] });
            }}
          >
            {t("撤销其他会话")}
          </Button>
        </div>
        <div className="ui-table-wrap">
          <table className="ui-table">
            <thead>
              <tr>
                <th>{t("创建时间")}</th>
                <th>{t("最近活动")}</th>
                <th>{t("来源")}</th>
                <th>{t("绝对到期")}</th>
                <th>{t("状态")}</th>
                <th>{t("操作")}</th>
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
                      {t("撤销")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {ordinaryUser ? (
        <Card className="detail-panel">
          <div className="section-card-header">
            <div>
              <h2>{t("CLI Tokens")}</h2>
              <div className="muted">
                {t("仅用于自己的Portal Job；Token明文只显示一次")}
              </div>
            </div>
          </div>
          <form onSubmit={submitCliToken}>
            <div className="form-field">
              <label htmlFor="cli-token-label">{t("Token名称")}</label>
              <Input
                id="cli-token-label"
                value={tokenLabel}
                maxLength={64}
                onChange={(event) => setTokenLabel(event.target.value)}
                placeholder={t("例如：开发容器")}
              />
            </div>
            <div className="form-field">
              <label htmlFor="cli-token-expiry">{t("有效期")}</label>
              <select
                id="cli-token-expiry"
                className="ui-input"
                value={tokenExpiry}
                onChange={(event) => setTokenExpiry(event.target.value)}
              >
                <option value="30">{t("30天")}</option>
                <option value="90">{t("90天")}</option>
                <option value="365">{t("365天")}</option>
                <option value="never">{t("永不过期")}</option>
              </select>
            </div>
            <div className="form-field">
              <label htmlFor="cli-token-password">{t("当前网页密码")}</label>
              <Input
                id="cli-token-password"
                type="password"
                autoComplete="current-password"
                value={tokenPassword}
                onChange={(event) => setTokenPassword(event.target.value)}
              />
            </div>
            <Button tone="primary" type="submit" disabled={tokenBusy}>
              {t("创建Token")}
            </Button>
          </form>
          {tokenMessage ? (
            <div className="notice" role="status">
              {tokenMessage}
            </div>
          ) : null}
          {createdToken ? (
            <div className="connection-output" role="status">
              <div className="field-label">
                {t("新CLI Token（仅显示一次）")}
              </div>
              <pre className="connection-command">{createdToken}</pre>
              <Button onClick={() => void copyText(createdToken)}>
                {t("复制Token")}
              </Button>
            </div>
          ) : null}
          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>{t("名称")}</th>
                  <th>{t("状态")}</th>
                  <th>{t("创建时间")}</th>
                  <th>{t("最近活动")}</th>
                  <th>{t("绝对到期")}</th>
                  <th>{t("操作")}</th>
                </tr>
              </thead>
              <tbody>
                {tokenQuery.data?.tokens.map((token) => (
                  <tr key={token.id}>
                    <td>{token.label}</td>
                    <td>
                      <StatusBadge value={token.state} />
                    </td>
                    <td>{token.created_at}</td>
                    <td>{token.last_used_at ?? "-"}</td>
                    <td>{token.expires_at ?? t("永不过期")}</td>
                    <td>
                      <Button
                        disabled={token.state !== "ACTIVE"}
                        onClick={async () => {
                          await revokeCliToken(token.id);
                          setCreatedToken(null);
                          await queryClient.invalidateQueries({
                            queryKey: ["cli-tokens"],
                          });
                        }}
                      >
                        {t("撤销")}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
    </>
  );
}
