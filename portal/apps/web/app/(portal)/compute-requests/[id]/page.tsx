"use client";

import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button, Card, Input, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../../components/PortalShell";
import {
  ApiError,
  adminComputeRequest,
  approveAndProvision,
  me,
  reauthenticate,
  retryProvision,
  reviewComputeRequest,
} from "../../../../lib/api";
import { randomUuid } from "../../../../lib/random-uuid";

const PROGRESS_STEPS = [
  ["PREPARING", "Preparing"],
  ["VALIDATING", "Validating"],
  ["CREATING_ENVIRONMENT", "Creating environment"],
  ["FINALIZING", "Finalizing"],
] as const;

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function safeFailureSummary(
  firstFailedStep?: string | null,
  code?: string | null,
) {
  if (firstFailedStep === "CONTAINER_IMAGE") {
    return "Container image validation failed";
  }
  if (code === "PROVISION_RUNTIME_CONTRACT_MISMATCH") {
    return "Runtime safety contract changed; Provision stopped before resource writes";
  }
  return code ? `Provision failed (${code})` : "Provision failed";
}

export default function ComputeRequestDetailPage() {
  const params = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const [reviewNote, setReviewNote] = useState("");
  const [approvalPassword, setApprovalPassword] = useState("");
  const [adminNote, setAdminNote] = useState("");
  const [retryPassword, setRetryPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["admin-compute-request", params.id],
    queryFn: () => adminComputeRequest(params.id),
    refetchInterval: (state) =>
      state.state.data?.request.lifecycle_state === "PROVISIONING"
        ? 1500
        : false,
  });
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["admin-compute-request", params.id],
      }),
      queryClient.invalidateQueries({ queryKey: ["admin-compute-requests"] }),
      queryClient.invalidateQueries({ queryKey: ["users"] }),
      queryClient.invalidateQueries({ queryKey: ["operations"] }),
      queryClient.invalidateQueries({ queryKey: ["me"] }),
    ]);
  };

  const approve = useMutation({
    mutationFn: async () => {
      if (current.data?.recent_auth_valid !== true) {
        await reauthenticate(approvalPassword);
      }
      return approveAndProvision(params.id, {
        review_note: reviewNote.trim() || null,
        idempotency_key: randomUuid(),
      });
    },
    onSuccess: async (result) => {
      setApprovalPassword("");
      setMessage(
        result.status === "KEY_ENROLLMENT_PENDING"
          ? "申请已批准，计算环境已自动创建；等待用户登记 Container SSH 公钥。"
          : result.status === "FAILED"
            ? "申请已批准，但 Provision 失败；系统已自动执行安全回滚检查。"
            : "申请已批准，Provision 正在后台执行。",
      );
      await refresh();
    },
  });

  const reject = useMutation({
    mutationFn: () =>
      reviewComputeRequest(params.id, {
        decision: "REJECT",
        review_note: reviewNote.trim(),
        idempotency_key: randomUuid(),
      }),
    onSuccess: async () => {
      setMessage("申请已拒绝；没有服务器资源受到影响。");
      await refresh();
    },
  });

  const retry = useMutation({
    mutationFn: async () => {
      if (current.data?.recent_auth_valid !== true) {
        await reauthenticate(retryPassword);
      }
      return retryProvision(params.id, {
        admin_note: adminNote.trim() || null,
        idempotency_key: randomUuid(),
      });
    },
    onSuccess: async (result) => {
      setRetryPassword("");
      setMessage(
        result.status === "KEY_ENROLLMENT_PENDING"
          ? "新的 Provision Attempt 已自动完成；历史失败 Attempt 保持不变。"
          : result.status === "FAILED"
            ? "新的 Provision Attempt 失败；系统已自动收敛回滚状态。"
            : "新的 Provision Attempt 正在执行。",
      );
      await refresh();
    },
  });

  const actionError = approve.error ?? reject.error ?? retry.error;
  const busy = approve.isPending || reject.isPending || retry.isPending;

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
  const attempts = item.attempts ?? [];
  const currentAttempt = attempts.length ? attempts[attempts.length - 1] : null;
  const lifecycleOperation =
    currentAttempt?.provision_operation ??
    currentAttempt?.stage_operation ??
    null;
  const failureOperation = lifecycleOperation;
  const lifecycleState = item.lifecycle_state ?? item.status;
  const recentAuthValid = current.data?.recent_auth_valid === true;
  const unknownResources = failureOperation?.unknown_resource_state ?? [];
  const workflowSteps = lifecycleOperation?.workflow_steps ?? {};
  const provisioning = lifecycleState === "PROVISIONING" || busy;

  return (
    <>
      <PageHeading
        title={`${item.username} · 计算资源申请`}
        description="标准计算环境 · 300GB Storage · Lease activation 后最多 96h"
        action={<StatusBadge value={lifecycleState} />}
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
        <SectionCard title="Portal 用户" subtitle="Identity">
          <dl className="kv-grid">
            <div className="kv">
              <dt>用户</dt>
              <dd>{account?.login_name ?? item.username}</dd>
            </div>
            <div className="kv">
              <dt>账号状态</dt>
              <dd>{account?.account_state ?? "UNKNOWN"}</dd>
            </div>
            <div className="kv">
              <dt>计算环境</dt>
              <dd>{account?.compute_state ?? "NOT PROVISIONED"}</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="标准资源" subtitle="Backend enforced">
          <dl className="kv-grid">
            <div className="kv">
              <dt>Container</dt>
              <dd>
                8 CPU / 32GB / GPU{" "}
                {item.requested_container_profile === "GPU_1_8CPU_32GB"
                  ? "1 (Slurm scheduled)"
                  : "NONE"}
              </dd>
            </div>
            <div className="kv">
              <dt>Slurm</dt>
              <dd>company / general / Max GPU {item.requested_gpu_max}</dd>
            </div>
            <div className="kv">
              <dt>Host SSH</dt>
              <dd>DISABLED</dd>
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
      </Card>

      {item.status === "REQUESTED" || item.status === "UNDER_REVIEW" ? (
        <Card className="detail-panel">
          <div className="detail-section-heading">
            <div>
              <h2>管理员审批</h2>
              <p className="muted">
                一次批准将由后端自动完成 Plan、Reservation、Dry-run 与 Stage。
              </p>
            </div>
            <StatusBadge value="HUMAN REVIEW" />
          </div>
          <div className="form-field">
            <label htmlFor="review-note">审批备注（可选；拒绝时必填）</label>
            <textarea
              id="review-note"
              className="ui-input identity-note"
              maxLength={1000}
              value={reviewNote}
              onChange={(event) => setReviewNote(event.target.value)}
            />
          </div>
          {!recentAuthValid ? (
            <div className="form-field">
              <label htmlFor="approval-password">管理员密码</label>
              <Input
                id="approval-password"
                type="password"
                autoComplete="current-password"
                value={approvalPassword}
                onChange={(event) => setApprovalPassword(event.target.value)}
              />
              <p className="muted">
                本次 recent-auth 同时授权批准与完整
                Provision，不会在内部步骤重复询问。
              </p>
            </div>
          ) : null}
          <div className="button-row">
            <Button
              data-testid="approve-and-provision"
              tone="primary"
              disabled={busy || (!recentAuthValid && !approvalPassword)}
              onClick={() => approve.mutate()}
            >
              {approve.isPending ? "Provisioning..." : "批准"}
            </Button>
            <Button
              disabled={busy || !reviewNote.trim()}
              onClick={() => reject.mutate()}
            >
              拒绝（需备注）
            </Button>
          </div>
        </Card>
      ) : null}

      {provisioning ? (
        <Card className="detail-panel" data-testid="provisioning-progress">
          <div className="detail-section-heading">
            <div>
              <h2>Provisioning...</h2>
              <p className="muted">浏览器无需协调内部步骤。</p>
            </div>
            <StatusBadge value="RUNNING" />
          </div>
          <div className="provision-progress">
            {PROGRESS_STEPS.map(([key, label]) => (
              <div className="kv" key={key}>
                <span>{label}</span>
                <StatusBadge
                  value={workflowSteps[key] ?? (busy ? "PENDING" : "WAITING")}
                />
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {item.status === "FAILED" ? (
        <Card className="detail-panel">
          <div className="detail-section-heading">
            <div>
              <h2>Provision 失败</h2>
              <p className="muted">
                {safeFailureSummary(
                  failureOperation?.first_failed_step,
                  failureOperation?.error_code,
                )}
              </p>
            </div>
            <StatusBadge value={item.retry_state ?? "FAILED"} />
          </div>
          <dl className="kv-grid">
            <div className="kv">
              <dt>Attempt</dt>
              <dd>#{currentAttempt?.attempt_number ?? "—"}</dd>
            </div>
            <div className="kv">
              <dt>回滚</dt>
              <dd>
                <StatusBadge
                  value={
                    failureOperation?.rollback_status ??
                    (item.retry_available
                      ? "ROLLED_BACK"
                      : "REQUIRES_MANUAL_REVIEW")
                  }
                />
              </dd>
            </div>
            <div className="kv">
              <dt>失败时间</dt>
              <dd>{localTime(failureOperation?.finished_at)}</dd>
            </div>
          </dl>

          {item.retry_available ? (
            <>
              <div className="form-field">
                <label htmlFor="admin-note">管理员备注（可选）</label>
                <textarea
                  id="admin-note"
                  className="ui-input identity-note"
                  maxLength={500}
                  value={adminNote}
                  onChange={(event) => setAdminNote(event.target.value)}
                />
              </div>
              {!recentAuthValid ? (
                <div className="form-field">
                  <label htmlFor="retry-password">管理员密码</label>
                  <Input
                    id="retry-password"
                    type="password"
                    autoComplete="current-password"
                    value={retryPassword}
                    onChange={(event) => setRetryPassword(event.target.value)}
                  />
                  <p className="muted">
                    Recent-auth 窗口已过期；本次认证与 Retry 使用同一个按钮。
                  </p>
                </div>
              ) : null}
              <Button
                data-testid="retry-provision"
                tone="primary"
                disabled={busy || (!recentAuthValid && !retryPassword)}
                onClick={() => retry.mutate()}
              >
                {retry.isPending ? "Provisioning..." : "重试 Provision"}
              </Button>
            </>
          ) : (
            <div
              className="error-box"
              data-testid="manual-review"
              role="status"
            >
              <strong>需要人工处理</strong>
              <p>系统无法权威确定以下资源状态，安全 Reservation 保持占用。</p>
              {unknownResources.length ? (
                <ul>
                  {unknownResources.map((resource) => (
                    <li key={resource}>{resource}</li>
                  ))}
                </ul>
              ) : (
                <p>等待 Root Worker 返回明确资源状态。</p>
              )}
            </div>
          )}
        </Card>
      ) : null}

      {plan && item.status !== "FAILED" ? (
        <Card className="detail-panel">
          <div className="detail-section-heading">
            <div>
              <h2>计算环境</h2>
              <p className="muted">
                Plan 由 Attempt 内部管理，无需管理员操作。
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
              <dt>开发容器</dt>
              <dd>{plan.container_name}</dd>
            </div>
            <div className="kv">
              <dt>Storage</dt>
              <dd>300GB</dd>
            </div>
            <div className="kv">
              <dt>Lease</dt>
              <dd>96h after activation · NOT STARTED</dd>
            </div>
          </dl>
        </Card>
      ) : null}

      {currentAttempt ? (
        <details className="detail-panel advanced-diagnostics">
          <summary>Advanced diagnostics</summary>
          <dl className="kv-grid">
            <div className="kv">
              <dt>Attempt</dt>
              <dd>#{currentAttempt.attempt_number}</dd>
            </div>
            <div className="kv">
              <dt>Operation</dt>
              <dd>{lifecycleOperation?.id ?? failureOperation?.id ?? "—"}</dd>
            </div>
            <div className="kv">
              <dt>Runtime</dt>
              <dd>{lifecycleOperation?.deployment_version ?? "historical"}</dd>
            </div>
            <div className="kv">
              <dt>Failed step</dt>
              <dd>{failureOperation?.first_failed_step ?? "—"}</dd>
            </div>
            <div className="kv">
              <dt>Error code</dt>
              <dd>{failureOperation?.error_code ?? "—"}</dd>
            </div>
            <div className="kv">
              <dt>Execution contract</dt>
              <dd>
                {lifecycleOperation?.canonical_execution_contract
                  ? "BOUND"
                  : "historical"}
              </dd>
            </div>
          </dl>
        </details>
      ) : null}
    </>
  );
}
