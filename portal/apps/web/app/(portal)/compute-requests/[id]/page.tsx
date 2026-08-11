"use client";

import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button, Card, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../../components/PortalShell";
import {
  ObjectTable,
  type SimpleColumnDef,
  type SimpleRow,
} from "../../../../components/Tables";
import {
  ApiError,
  adminComputeRequest,
  createProvisionPlan,
  dryRunProvisionPlan,
  reviewComputeRequest,
} from "../../../../lib/api";
import { randomUuid } from "../../../../lib/random-uuid";

const VALIDATION_COLUMNS: SimpleColumnDef[] = [
  { accessorKey: "check", header: "检查项" },
  {
    accessorKey: "status",
    header: "结果",
    cell: (info) => <StatusBadge value={String(info.getValue())} />,
  },
  { accessorKey: "detail", header: "说明" },
];

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asRows(value: unknown): SimpleRow[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is SimpleRow =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function ComputeRequestDetailPage() {
  const params = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const [reviewNote, setReviewNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["admin-compute-request", params.id],
    queryFn: () => adminComputeRequest(params.id),
  });
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["admin-compute-request", params.id],
      }),
      queryClient.invalidateQueries({ queryKey: ["admin-compute-requests"] }),
      queryClient.invalidateQueries({ queryKey: ["users"] }),
      queryClient.invalidateQueries({ queryKey: ["operations"] }),
    ]);
  };
  const review = useMutation({
    mutationFn: (decision: "APPROVE" | "REJECT") =>
      reviewComputeRequest(params.id, {
        decision,
        review_note: reviewNote.trim() || null,
        idempotency_key: randomUuid(),
      }),
    onSuccess: async (result) => {
      setMessage(
        result.status === "APPROVED"
          ? "申请已批准；尚未执行 Provision，也未启动 Lease。"
          : "申请已拒绝；没有服务器资源受到影响。",
      );
      await refresh();
    },
  });
  const planMutation = useMutation({
    mutationFn: () => createProvisionPlan(params.id),
    onSuccess: async () => {
      setMessage("Provision Plan 已生成并 reservation；宿主资源仍未创建。");
      await refresh();
    },
  });
  const dryRun = useMutation({
    mutationFn: () => dryRunProvisionPlan(params.id),
    onSuccess: async () => {
      setMessage(
        "Dry-run 已通过：READY_FOR_PROVISION；真实执行仍被下一 Gate 禁用。",
      );
      await refresh();
    },
  });
  const actionError = review.error ?? planMutation.error ?? dryRun.error;

  if (query.isPending) {
    return (
      <>
        <PageHeading title="计算资源申请详情" />
        <LoadingBlock />
      </>
    );
  }
  if (query.isError) {
    return (
      <>
        <PageHeading title="计算资源申请详情" />
        <ErrorBlock message="申请不存在，或当前账号无权查看。" />
      </>
    );
  }
  const item = query.data.request;
  const account = item.portal_user;
  const plan = item.plan;
  const validationSource = asRecord(
    plan?.dry_run_result ?? plan?.allocator_result,
  );
  const validations = asRows(validationSource.validation_results);
  const busy = review.isPending || planMutation.isPending || dryRun.isPending;

  return (
    <>
      <PageHeading
        title={`${item.username} · 计算资源申请`}
        description={`Request ID ${item.id}`}
        action={<StatusBadge value={item.status} />}
      />
      {message ? (
        <div className="notice" role="status">
          {message}
        </div>
      ) : null}
      {actionError ? (
        <div className="error-box" role="alert">
          {actionError instanceof ApiError
            ? actionError.message
            : "管理员操作失败"}
        </div>
      ) : null}
      <div className="section-grid">
        <SectionCard title="Portal 用户" subtitle="Identity Layer">
          <dl className="kv-grid">
            <div className="kv">
              <dt>Login</dt>
              <dd>{account?.login_name ?? item.username}</dd>
            </div>
            <div className="kv">
              <dt>Role</dt>
              <dd>{account?.role ?? "user"}</dd>
            </div>
            <div className="kv">
              <dt>Portal State</dt>
              <dd>{account?.account_state ?? "UNKNOWN"}</dd>
            </div>
            <div className="kv">
              <dt>Password State</dt>
              <dd>{account?.password_state ?? "UNKNOWN"}</dd>
            </div>
            <div className="kv">
              <dt>Compute</dt>
              <dd>NOT PROVISIONED</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="Requested" subtitle="用户可见资源规格">
          <dl className="kv-grid">
            <div className="kv">
              <dt>GPU Max</dt>
              <dd>{item.requested_gpu_max}</dd>
            </div>
            <div className="kv">
              <dt>Storage</dt>
              <dd>300GB STANDARD</dd>
            </div>
            <div className="kv">
              <dt>Container</dt>
              <dd>8 CPU / 32GB / GPU NONE</dd>
            </div>
            <div className="kv">
              <dt>Lease</dt>
              <dd>96h after activation</dd>
            </div>
            <div className="kv">
              <dt>Submitted</dt>
              <dd>{localTime(item.submitted_at)}</dd>
            </div>
          </dl>
        </SectionCard>
      </div>
      <Card className="detail-panel">
        <h2>申请说明</h2>
        <p>{item.purpose}</p>
        {item.user_note ? (
          <p className="muted">备注：{item.user_note}</p>
        ) : null}
        {item.review_note ? (
          <div className="notice">审批备注：{item.review_note}</div>
        ) : null}
      </Card>
      {item.status === "REQUESTED" || item.status === "UNDER_REVIEW" ? (
        <Card className="detail-panel">
          <div className="detail-section-heading">
            <div>
              <h2>管理员审批</h2>
              <p className="muted">批准不会立即执行 Provision。</p>
            </div>
            <StatusBadge value="HUMAN REVIEW" />
          </div>
          <div className="form-field">
            <label htmlFor="review-note">审批备注</label>
            <textarea
              id="review-note"
              className="ui-input identity-note"
              maxLength={1000}
              value={reviewNote}
              onChange={(event) => setReviewNote(event.target.value)}
            />
          </div>
          <div className="button-row">
            <Button
              tone="primary"
              disabled={busy}
              onClick={() => review.mutate("APPROVE")}
            >
              批准
            </Button>
            <Button
              disabled={busy || !reviewNote.trim()}
              onClick={() => review.mutate("REJECT")}
            >
              拒绝（需备注）
            </Button>
          </div>
        </Card>
      ) : null}
      {item.status === "APPROVED" ? (
        <Card className="detail-panel">
          <h2>Provision Plan</h2>
          <p>
            allocator 将查验真实宿主冲突并在 Portal DB 建立有期限 reservation。
          </p>
          <Button
            tone="primary"
            disabled={busy}
            onClick={() => planMutation.mutate()}
          >
            生成 Provision Plan
          </Button>
        </Card>
      ) : null}
      {plan ? (
        <Card className="detail-panel">
          <div className="detail-section-heading">
            <div>
              <h2>资源计划</h2>
              <p className="muted">
                Reservation 有效至 {localTime(plan.reservation_expires_at)}
              </p>
            </div>
            <StatusBadge value={plan.state} />
          </div>
          <dl className="kv-grid">
            <div className="kv">
              <dt>用户名</dt>
              <dd>{plan.username}</dd>
            </div>
            <div className="kv">
              <dt>UID / GID</dt>
              <dd>
                {plan.uid} / {plan.gid}
              </dd>
            </div>
            <div className="kv">
              <dt>Project ID</dt>
              <dd>{plan.project_id}</dd>
            </div>
            <div className="kv">
              <dt>Storage</dt>
              <dd>300GB</dd>
            </div>
            <div className="kv">
              <dt>开发容器</dt>
              <dd>{plan.container_name}</dd>
            </div>
            <div className="kv">
              <dt>SSH Port</dt>
              <dd>{plan.container_ssh_port}</dd>
            </div>
            <div className="kv">
              <dt>Container</dt>
              <dd>8 CPU / 32GB / 4096 PIDs / GPU NONE</dd>
            </div>
            <div className="kv">
              <dt>Slurm</dt>
              <dd>company / general / Max GPU {plan.gpu_max}</dd>
            </div>
            <div className="kv">
              <dt>Lease</dt>
              <dd>96h · NOT STARTED</dd>
            </div>
            <div className="kv">
              <dt>Host SSH</dt>
              <dd>DISABLED</dd>
            </div>
            <div className="kv">
              <dt>Shell</dt>
              <dd>{plan.shell}</dd>
            </div>
            <div className="kv">
              <dt>Password</dt>
              <dd>{plan.password_state}</dd>
            </div>
          </dl>
          {validations.length ? (
            <>
              <h3 className="subheading">Dry-run / Allocator checks</h3>
              <ObjectTable rows={validations} columns={VALIDATION_COLUMNS} />
            </>
          ) : null}
          <div className="button-row">
            {plan.state === "RESERVED" ? (
              <Button
                tone="primary"
                disabled={busy}
                onClick={() => dryRun.mutate()}
              >
                执行 Dry-run
              </Button>
            ) : null}
            <Button disabled aria-label="正式创建计算环境等待下一 Gate">
              正式创建计算环境
            </Button>
          </div>
          <div className="notice">
            PROVISION EXECUTION: DISABLED — NEXT GATE。Lease starts_at /
            expires_at 保持为空。
          </div>
        </Card>
      ) : null}
      <div className="section-grid">
        <SectionCard title="资源零副作用">
          当前不会执行 useradd、docker create、xfs_quota、sacctmgr 或 systemd
          policy 写入。
        </SectionCard>
        <SectionCard title="SSH 策略">
          Host SSH 永久禁用；未来只允许用户自己的 Container SSH 公钥。
        </SectionCard>
      </div>
    </>
  );
}
