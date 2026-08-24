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
  const [message, setMessage] = useState<string | null>(null);
  const decision = useMutation({
    mutationFn: ({ id, value }: { id: string; value: "APPROVE" | "REJECT" }) =>
      decideRestoreRequest(id, value, comment.trim() || null),
    onSuccess: async (result) => {
      setMessage(
        result.status === "RESTORED"
          ? "恢复已完成；新 Lease 已按平台硬上限创建。"
          : result.status === "REJECTED"
            ? "恢复申请已拒绝。"
            : `恢复状态：${result.status}`,
      );
      setComment("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin-restore-requests"] }),
        queryClient.invalidateQueries({ queryKey: ["users"] }),
        queryClient.invalidateQueries({ queryKey: ["operations"] }),
      ]);
    },
  });
  const query = useQuery({
    queryKey: ["admin-restore-requests"],
    queryFn: adminRestoreRequests,
    refetchInterval: 20_000,
  });

  if (query.isPending) return <LoadingBlock />;
  if (query.isError) {
    return <ErrorBlock message="恢复申请暂时不可用，未执行任何操作。" />;
  }
  return (
    <>
      <PageHeading
        title="历史恢复申请"
        description="仅处理升级前已存在的待审批记录；新恢复由资源所有者自助执行"
        action={<StatusBadge value={`${query.data.count} REQUESTS`} />}
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
      {!query.data.requests.filter((row) => row.state === "REQUESTED")
        .length ? (
        <Card>
          <p className="muted">当前没有待处理的恢复申请。</p>
        </Card>
      ) : null}
      {query.data.requests
        .filter((row) => row.state === "REQUESTED")
        .map((row) => (
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
            <div className="form-field">
              <label htmlFor={`restore-comment-${row.id}`}>备注（可选）</label>
              <Input
                id={`restore-comment-${row.id}`}
                value={comment}
                maxLength={500}
                onChange={(event) => setComment(event.target.value)}
              />
            </div>
            <div className="button-row">
              <Button
                tone="primary"
                disabled={decision.isPending}
                onClick={() =>
                  decision.mutate({ id: row.id, value: "APPROVE" })
                }
              >
                批准恢复
              </Button>
              <Button
                disabled={decision.isPending}
                onClick={() => decision.mutate({ id: row.id, value: "REJECT" })}
              >
                拒绝
              </Button>
            </div>
          </SectionCard>
        ))}
    </>
  );
}
