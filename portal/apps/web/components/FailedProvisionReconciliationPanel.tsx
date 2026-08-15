"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button, Card, Input, StatusBadge } from "@h100-portal/ui";
import {
  ApiError,
  type ComputeResourceRequest,
  adminComputeRequest,
  me,
  reauthenticate,
  reconcileFailedProvision,
} from "../lib/api";

const RESERVATION_KINDS = [
  "UID",
  "GID",
  "PROJECT_ID",
  "SSH_PORT",
  "CONTAINER_NAME",
] as const;

type ReconciliationBinding = {
  requestId: string;
  username: string;
  attemptNumber: number;
  planId: string;
  stageOperationId: string;
  rollbackStatus: "REQUIRES_MANUAL_REVIEW";
  reservations: Array<{
    kind: (typeof RESERVATION_KINDS)[number];
    id: string;
    state: "FAILED_HOLD";
    value: string;
  }>;
};

function reservationValue(
  request: ComputeResourceRequest,
  kind: (typeof RESERVATION_KINDS)[number],
): string {
  const plan = request.plan;
  if (!plan) return "—";
  switch (kind) {
    case "UID":
      return String(plan.uid ?? "—");
    case "GID":
      return String(plan.gid ?? "—");
    case "PROJECT_ID":
      return String(plan.project_id ?? "—");
    case "SSH_PORT":
      return String(plan.container_ssh_port ?? "—");
    case "CONTAINER_NAME":
      return plan.container_name ?? "—";
  }
}

export function failedProvisionReconciliationBinding(
  request: ComputeResourceRequest,
): ReconciliationBinding | null {
  const plan = request.plan;
  const attempt = request.attempts?.find(
    (candidate) => candidate.plan.id === plan?.id,
  );
  const stage = attempt?.stage_operation;
  if (
    request.status !== "FAILED" ||
    request.managed_user_id ||
    !plan ||
    plan.state !== "FAILED" ||
    !attempt ||
    stage?.status !== "FAILED" ||
    stage.rollback_status !== "REQUIRES_MANUAL_REVIEW"
  ) {
    return null;
  }
  const reservations = RESERVATION_KINDS.map((kind) => {
    const row = attempt.reservations[kind];
    return row?.state === "FAILED_HOLD"
      ? {
          kind,
          id: row.id,
          state: "FAILED_HOLD" as const,
          value: reservationValue(request, kind),
        }
      : null;
  });
  if (reservations.some((row) => row === null)) return null;
  return {
    requestId: request.id,
    username: request.username,
    attemptNumber: attempt.attempt_number,
    planId: plan.id,
    stageOperationId: stage.id,
    rollbackStatus: "REQUIRES_MANUAL_REVIEW",
    reservations: reservations.filter((row) => row !== null),
  };
}

function sameBinding(
  left: ReconciliationBinding,
  right: ReconciliationBinding | null,
) {
  return Boolean(
    right &&
    left.requestId === right.requestId &&
    left.attemptNumber === right.attemptNumber &&
    left.planId === right.planId &&
    left.stageOperationId === right.stageOperationId &&
    left.reservations.every(
      (reservation, index) =>
        reservation.id === right.reservations[index]?.id &&
        right.reservations[index]?.state === "FAILED_HOLD",
    ),
  );
}

export function FailedProvisionReconciliationPanel({
  request,
}: {
  request: ComputeResourceRequest;
}) {
  const queryClient = useQueryClient();
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });
  const binding = failedProvisionReconciliationBinding(request);
  const [requestConfirmation, setRequestConfirmation] = useState("");
  const [attemptConfirmation, setAttemptConfirmation] = useState("");
  const [planConfirmation, setPlanConfirmation] = useState("");
  const [stageConfirmation, setStageConfirmation] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [password, setPassword] = useState("");
  const [consequencesConfirmed, setConsequencesConfirmed] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const reconciliation = useMutation({
    mutationFn: async () => {
      if (!binding) {
        throw new ApiError(
          409,
          "PROVISION_RECONCILIATION_NOT_ELIGIBLE",
          "失败 Attempt 已不再满足正式对账条件，请刷新页面。",
        );
      }
      await reauthenticate(password);
      const fresh = await adminComputeRequest(binding.requestId);
      const freshBinding = failedProvisionReconciliationBinding(fresh.request);
      if (!sameBinding(binding, freshBinding)) {
        throw new ApiError(
          409,
          "PROVISION_RECONCILIATION_STALE_VIEW",
          "Attempt、Stage 或 FAILED_HOLD 已变化；未提交 Reconcile，请重新核对。",
        );
      }
      return reconcileFailedProvision(binding.requestId, {
        plan_id: binding.planId,
        failed_stage_operation_id: binding.stageOperationId,
        review_note: reviewNote.trim(),
      });
    },
    onSuccess: async (result) => {
      setMessage(
        `Reconcile Operation ${result.operation_id} 已完成；Rollback VERIFIED，FAILED_HOLD 已释放，未创建 Attempt #3。`,
      );
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["admin-compute-request", request.id],
        }),
        queryClient.invalidateQueries({ queryKey: ["admin-compute-requests"] }),
        queryClient.invalidateQueries({ queryKey: ["operations"] }),
      ]);
    },
    onSettled: () => setPassword(""),
  });

  if (!binding || current.data?.role !== "platform_owner") return null;

  const confirmationsMatch =
    requestConfirmation === binding.requestId &&
    attemptConfirmation === String(binding.attemptNumber) &&
    planConfirmation === binding.planId &&
    stageConfirmation === binding.stageOperationId;
  const ready =
    confirmationsMatch &&
    reviewNote.trim().length > 0 &&
    password.length > 0 &&
    consequencesConfirmed;
  const error = reconciliation.error;

  return (
    <Card className="detail-panel">
      <div className="detail-section-heading">
        <div>
          <h2>失败 Provision 正式对账</h2>
          <p className="muted">
            这是 Verify Rollback / Release Failed Hold，不是 Retry Stage。
          </p>
        </div>
        <StatusBadge value="MANUAL RECONCILIATION" />
      </div>
      <div className="notice notice-danger">
        Root Worker 会在正式提交时 fresh 验证宿主零残留；任一资源 PRESENT 或
        UNKNOWN 都会 fail closed，并保持全部
        FAILED_HOLD。浏览器不会把部署验收结果当作权限边界。
      </div>
      <dl className="kv-grid">
        <div className="kv">
          <dt>User</dt>
          <dd>{binding.username}</dd>
        </div>
        <div className="kv">
          <dt>Attempt</dt>
          <dd>#{binding.attemptNumber} / FAILED</dd>
        </div>
        <div className="kv">
          <dt>Plan</dt>
          <dd>{binding.planId}</dd>
        </div>
        <div className="kv">
          <dt>Stage</dt>
          <dd>{binding.stageOperationId}</dd>
        </div>
        <div className="kv">
          <dt>Rollback</dt>
          <dd>{binding.rollbackStatus}</dd>
        </div>
        <div className="kv">
          <dt>Fresh Host Residue</dt>
          <dd>提交时由 Root Worker 重新验证</dd>
        </div>
      </dl>
      <h3 className="subheading">FAILED_HOLD Reservations</h3>
      <dl className="kv-grid">
        {binding.reservations.map((reservation) => (
          <div className="kv" key={reservation.kind}>
            <dt>{reservation.kind}</dt>
            <dd>
              {reservation.value} · {reservation.id} · {reservation.state}
            </dd>
          </div>
        ))}
      </dl>
      <div className="notice">
        Expected：Attempt #2 保持 FAILED，Rollback → VERIFIED，5 条 Reservation
        → RELEASED，Request → RETRY_ELIGIBLE；不会创建 Attempt #3，不会执行
        Provision。
      </div>
      <div className="form-field">
        <label htmlFor="reconcile-request-confirmation">
          输入完整 Request ID 确认
        </label>
        <Input
          id="reconcile-request-confirmation"
          autoComplete="off"
          value={requestConfirmation}
          onChange={(event) =>
            setRequestConfirmation(event.target.value.trim())
          }
        />
      </div>
      <div className="form-field">
        <label htmlFor="reconcile-attempt-confirmation">
          输入 Attempt 编号确认
        </label>
        <Input
          id="reconcile-attempt-confirmation"
          inputMode="numeric"
          autoComplete="off"
          value={attemptConfirmation}
          onChange={(event) =>
            setAttemptConfirmation(event.target.value.trim())
          }
        />
      </div>
      <div className="form-field">
        <label htmlFor="reconcile-plan-confirmation">
          输入完整 Plan ID 确认
        </label>
        <Input
          id="reconcile-plan-confirmation"
          autoComplete="off"
          value={planConfirmation}
          onChange={(event) => setPlanConfirmation(event.target.value.trim())}
        />
      </div>
      <div className="form-field">
        <label htmlFor="reconcile-stage-confirmation">
          输入完整 Stage Operation ID 确认
        </label>
        <Input
          id="reconcile-stage-confirmation"
          autoComplete="off"
          value={stageConfirmation}
          onChange={(event) => setStageConfirmation(event.target.value.trim())}
        />
      </div>
      <div className="form-field">
        <label htmlFor="reconcile-review-note">人工对账说明</label>
        <textarea
          id="reconcile-review-note"
          className="ui-input identity-note"
          maxLength={500}
          value={reviewNote}
          onChange={(event) => setReviewNote(event.target.value)}
        />
      </div>
      <label className="confirmation-check" htmlFor="reconcile-consequences">
        <input
          id="reconcile-consequences"
          type="checkbox"
          checked={consequencesConfirmed}
          onChange={(event) => setConsequencesConfirmed(event.target.checked)}
        />
        我确认这不是重试：Attempt #2 保持失败，仅在零残留验证通过后形式化验证
        Rollback 并释放 FAILED_HOLD；不会创建 Attempt #3。
      </label>
      <div className="form-field">
        <label htmlFor="reconcile-password">
          platform_owner 密码（recent re-authentication）
        </label>
        <Input
          id="reconcile-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </div>
      <div className="button-row">
        <Button
          tone="danger"
          disabled={!ready || reconciliation.isPending}
          onClick={() => reconciliation.mutate()}
        >
          Reconcile Failed Provision
        </Button>
        <span className="muted">只点击一次；不要点击 Retry Stage。</span>
      </div>
      {error ? (
        <div className="error-box" role="alert">
          {error instanceof ApiError
            ? error.message
            : "正式对账失败；状态未改变。"}
        </div>
      ) : null}
      {message ? (
        <div className="success-notice" role="status">
          {message}
        </div>
      ) : null}
    </Card>
  );
}
