"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button, Card, Input, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../components/PortalShell";
import {
  type AdminLeaseRenewalRequest,
  ApiError,
  adminLeaseRenewals,
  decideLeaseRenewal,
  me,
  reauthenticate,
} from "../../../lib/api";

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

function RenewalRequestCard({
  row,
  canReview,
  recentAuthValid,
}: {
  row: AdminLeaseRenewalRequest;
  canReview: boolean;
  recentAuthValid: boolean;
}) {
  const queryClient = useQueryClient();
  const [comment, setComment] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const decision = useMutation({
    mutationFn: async (value: "APPROVE" | "REJECT") => {
      if (!recentAuthValid) await reauthenticate(password);
      return decideLeaseRenewal(row.id, value, comment.trim() || null);
    },
    onSuccess: async (result) => {
      setPassword("");
      setComment("");
      setMessage(
        result.status === "APPROVED"
          ? "续期已批准；后续 Lease 将在当前 Lease 到期时自动生效。"
          : "续期申请已拒绝。",
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin-lease-renewals"] }),
        queryClient.invalidateQueries({ queryKey: ["users"] }),
        queryClient.invalidateQueries({ queryKey: ["me"] }),
      ]);
    },
  });
  const passwordMissing = !recentAuthValid && !password;

  return (
    <SectionCard
      title={`${row.username} · ${row.display_name}`}
      subtitle={`申请 ${Math.round(row.duration_seconds / 3600)} 小时`}
      action={<StatusBadge value={row.actionable ? "待审批" : row.state} />}
    >
      <dl className="kv-grid">
        <div className="kv">
          <dt>申请时间</dt>
          <dd>{localTime(row.requested_at)}</dd>
        </div>
        <div className="kv">
          <dt>当前 Lease 到期</dt>
          <dd>{localTime(row.lease_expires_at)}</dd>
        </div>
        <div className="kv">
          <dt>Lease 状态</dt>
          <dd>{row.lease_state}</dd>
        </div>
        <div className="kv">
          <dt>审批策略快照</dt>
          <dd>{row.approval_required ? "需要管理员审批" : "自动批准"}</dd>
        </div>
      </dl>

      {message ? (
        <div className="notice" role="status">
          {message}
        </div>
      ) : null}
      {decision.error ? (
        <div className="error-box" role="alert">
          {decision.error instanceof ApiError
            ? decision.error.message
            : "续期审批失败，未改变申请。"}
        </div>
      ) : null}

      {row.actionable && canReview ? (
        <>
          <div className="form-field">
            <label htmlFor={`renewal-comment-${row.id}`}>
              审批备注（可选）
            </label>
            <Input
              id={`renewal-comment-${row.id}`}
              value={comment}
              maxLength={500}
              onChange={(event) => setComment(event.target.value)}
            />
          </div>
          {!recentAuthValid ? (
            <div className="form-field">
              <label htmlFor={`renewal-password-${row.id}`}>管理员密码</label>
              <Input
                id={`renewal-password-${row.id}`}
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <p className="muted">审批前必须完成 recent re-authentication。</p>
            </div>
          ) : null}
          <div className="button-row">
            <Button
              tone="primary"
              disabled={decision.isPending || passwordMissing}
              onClick={() => decision.mutate("APPROVE")}
            >
              批准续期
            </Button>
            <Button
              disabled={decision.isPending || passwordMissing}
              onClick={() => decision.mutate("REJECT")}
            >
              拒绝
            </Button>
          </div>
        </>
      ) : row.actionable ? (
        <p className="muted">当前角色仅可查看，不能处理续期申请。</p>
      ) : (
        <p className="muted">
          {row.closed_reason === "LEASE_EXPIRED_RESTORE_REQUIRED"
            ? "原 Lease 已到期，该申请不可再审批；用户应使用恢复流程。"
            : `处理时间：${localTime(row.decided_at)}`}
        </p>
      )}
    </SectionCard>
  );
}

export default function LeaseRenewalsPage() {
  const query = useQuery({
    queryKey: ["admin-lease-renewals"],
    queryFn: adminLeaseRenewals,
    refetchInterval: 20_000,
  });
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });

  if (query.isPending || current.isPending) return <LoadingBlock />;
  if (query.isError || current.isError) {
    return <ErrorBlock message="续期审批列表不可用，未执行任何操作。" />;
  }

  const canReview = ["platform_owner", "platform_admin"].includes(
    current.data.role,
  );
  const recentAuthValid = current.data.recent_auth_valid === true;
  const pending = query.data.requests.filter((row) => row.actionable);
  const history = query.data.requests.filter((row) => !row.actionable);

  return (
    <>
      <PageHeading
        title="续期审批"
        description="处理资源到期前提交的 Lease 续期；资源到期后的操作请使用恢复流程。"
        action={<StatusBadge value={`${pending.length} 待审批`} />}
      />
      {!pending.length ? (
        <Card>
          <p className="muted">当前没有可处理的续期申请。</p>
        </Card>
      ) : null}
      {pending.map((row) => (
        <RenewalRequestCard
          key={row.id}
          row={row}
          canReview={canReview}
          recentAuthValid={recentAuthValid}
        />
      ))}
      {history.length ? (
        <SectionCard title="历史记录" subtitle="已处理或已失效">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>用户</th>
                  <th>状态</th>
                  <th>申请时间</th>
                  <th>处理时间</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {history.map((row) => (
                  <tr key={row.id}>
                    <td>{row.username}</td>
                    <td>
                      <StatusBadge
                        value={
                          row.closed_reason === "LEASE_EXPIRED_RESTORE_REQUIRED"
                            ? "已失效"
                            : row.state
                        }
                      />
                    </td>
                    <td>{localTime(row.requested_at)}</td>
                    <td>{localTime(row.decided_at)}</td>
                    <td>
                      {row.closed_reason === "LEASE_EXPIRED_RESTORE_REQUIRED"
                        ? "原 Lease 已到期，请使用恢复流程"
                        : row.decision_comment || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </SectionCard>
      ) : null}
    </>
  );
}
