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
  ApiError,
  adminRestoreRequests,
  decideRestoreRequest,
  me,
  reauthenticate,
} from "../../../lib/api";

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function RestoreRequestsPage() {
  const queryClient = useQueryClient();
  const [comment, setComment] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });
  const decision = useMutation({
    mutationFn: async ({
      id,
      value,
    }: {
      id: string;
      value: "APPROVE" | "REJECT";
    }) => {
      if (current.data?.recent_auth_valid !== true) {
        await reauthenticate(password);
      }
      return decideRestoreRequest(id, value, comment.trim() || null);
    },
    onSuccess: async (result) => {
      setMessage(
        result.status === "RESTORED"
          ? "恢复已完成；新 Lease 已按平台硬上限创建。"
          : result.status === "REJECTED"
            ? "恢复申请已拒绝。"
            : `恢复状态：${result.status}`,
      );
      setPassword("");
      setComment("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin-restore-requests"] }),
        queryClient.invalidateQueries({ queryKey: ["users"] }),
        queryClient.invalidateQueries({ queryKey: ["operations"] }),
        queryClient.invalidateQueries({ queryKey: ["me"] }),
      ]);
    },
  });
  const query = useQuery({
    queryKey: ["admin-restore-requests"],
    queryFn: adminRestoreRequests,
    refetchInterval: 20_000,
  });

  if (query.isPending || current.isPending) return <LoadingBlock />;
  if (query.isError || current.isError) {
    return <ErrorBlock message="恢复申请暂时不可用，未执行任何操作。" />;
  }
  const canReview = ["platform_owner", "platform_admin"].includes(
    current.data.role,
  );
  const recentAuthValid = current.data.recent_auth_valid === true;
  const pending = query.data.requests.filter(
    (row) => row.state === "REQUESTED",
  );
  const history = query.data.requests.filter(
    (row) => row.state !== "REQUESTED",
  );
  return (
    <>
      <PageHeading
        title="恢复审批"
        description="处理与用户续期审批策略绑定的回收站恢复申请"
        action={<StatusBadge value={`${pending.length} 待审批`} />}
      />
      {message ? (
        <div className="notice" role="status">
          {message}
        </div>
      ) : null}
      {decision.error ? (
        <div className="error-box" role="alert">
          {decision.error instanceof ApiError
            ? decision.error.message
            : "恢复操作失败"}
        </div>
      ) : null}
      {!pending.length ? (
        <Card>
          <p className="muted">当前没有待处理的恢复申请。</p>
        </Card>
      ) : null}
      {pending.map((row) => (
        <SectionCard
          key={row.id}
          title={`${row.username} · ${row.resource_name}`}
          action={<StatusBadge value="REQUESTED" />}
        >
          <dl className="kv-grid">
            <div className="kv">
              <dt>申请时间</dt>
              <dd>{localTime(row.requested_at)}</dd>
            </div>
            <div className="kv">
              <dt>Lease</dt>
              <dd>{Math.round(row.duration_seconds / 3600)}h（最多 96h）</dd>
            </div>
            <div className="kv">
              <dt>安全边界</dt>
              <dd>Host SSH disabled · public key policy preserved</dd>
            </div>
          </dl>
          {canReview ? (
            <>
              <div className="form-field">
                <label htmlFor={`restore-comment-${row.id}`}>
                  备注（可选）
                </label>
                <Input
                  id={`restore-comment-${row.id}`}
                  value={comment}
                  maxLength={500}
                  onChange={(event) => setComment(event.target.value)}
                />
              </div>
              {!recentAuthValid ? (
                <div className="form-field">
                  <label htmlFor={`restore-password-${row.id}`}>
                    管理员密码
                  </label>
                  <Input
                    id={`restore-password-${row.id}`}
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                  />
                  <p className="muted">
                    审批前必须完成 recent re-authentication。
                  </p>
                </div>
              ) : null}
              <div className="button-row">
                <Button
                  tone="primary"
                  disabled={
                    decision.isPending || (!recentAuthValid && !password)
                  }
                  onClick={() =>
                    decision.mutate({ id: row.id, value: "APPROVE" })
                  }
                >
                  批准恢复
                </Button>
                <Button
                  disabled={
                    decision.isPending || (!recentAuthValid && !password)
                  }
                  onClick={() =>
                    decision.mutate({ id: row.id, value: "REJECT" })
                  }
                >
                  拒绝
                </Button>
              </div>
            </>
          ) : (
            <p className="muted">当前角色仅可查看，不能处理恢复申请。</p>
          )}
        </SectionCard>
      ))}
      {history.length ? (
        <SectionCard title="历史记录" subtitle="已恢复、拒绝、取消或失败">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>用户</th>
                  <th>资源</th>
                  <th>状态</th>
                  <th>申请时间</th>
                  <th>处理时间</th>
                </tr>
              </thead>
              <tbody>
                {history.map((row) => (
                  <tr key={row.id}>
                    <td>{row.username}</td>
                    <td>{row.resource_name}</td>
                    <td>
                      <StatusBadge value={row.state} />
                    </td>
                    <td>{localTime(row.requested_at)}</td>
                    <td>{localTime(row.decided_at)}</td>
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
