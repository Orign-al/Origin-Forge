"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button, Input, StatusBadge } from "@h100-portal/ui";
import {
  ApiError,
  type AdminLeaseRecoveryIncident,
  type LeaseRecoveryOperationResult,
  type User,
  adminLeaseRecoveryIncidents,
  me,
  reauthenticate,
  retryLeaseRecycle,
  updateLeaseRenewalPolicy,
} from "../lib/api";

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

function managedUsername(user: User): string | null {
  const value = user.linux_identity?.unix_username;
  return typeof value === "string" && value ? value : null;
}

export function incidentForUser(
  incidents: AdminLeaseRecoveryIncident[],
  user: User,
): AdminLeaseRecoveryIncident | null {
  const username = managedUsername(user);
  return (
    incidents.find(
      (incident) =>
        incident.portal_user_id === user.id ||
        (username !== null && incident.username === username),
    ) ?? null
  );
}

export function recoveryEligible(
  incident: AdminLeaseRecoveryIncident | null,
): incident is AdminLeaseRecoveryIncident {
  return Boolean(
    incident?.time_expired &&
    incident.operation_status === "FAILED" &&
    incident.recycle_state === "FAILED" &&
    incident.manual_review_required &&
    incident.recovery_available &&
    !incident.data_delete_allowed,
  );
}

export function LeaseLifecyclePanel({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });
  const isPlatformOwner = current.data?.role === "platform_owner";
  const canManageRenewalPolicy = ["platform_owner", "platform_admin"].includes(
    current.data?.role ?? "",
  );
  const incidentQuery = useQuery({
    queryKey: ["admin-lease-recovery-incidents"],
    queryFn: adminLeaseRecoveryIncidents,
    enabled: isPlatformOwner,
    retry: false,
  });
  const incident = incidentForUser(incidentQuery.data?.incidents ?? [], user);
  const lifecycle = user.compute_lifecycle;
  const leaseId = incident?.lease_id ?? lifecycle?.lease_id ?? null;
  const leaseOwner =
    incident?.owner ?? lifecycle?.owner ?? managedUsername(user);
  const startsAt = incident?.starts_at ?? lifecycle?.starts_at ?? null;
  const expiresAt = incident?.expires_at ?? lifecycle?.expires_at ?? null;
  const timeExpired =
    incident?.time_expired ?? lifecycle?.time_expired ?? false;
  const leaseState = incident?.lease_state ?? lifecycle?.lease_state ?? null;
  const anomaly = timeExpired && leaseState === "ACTIVE";
  const canRecover = isPlatformOwner && recoveryEligible(incident);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [safeReason, setSafeReason] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [policyMessage, setPolicyMessage] = useState<string | null>(null);
  const [operation, setOperation] =
    useState<LeaseRecoveryOperationResult | null>(null);

  function closeConfirmation() {
    setConfirmationOpen(false);
    setConfirmation("");
    setSafeReason("");
    setPassword("");
  }

  async function openConfirmation() {
    setMessage(null);
    const refreshed = await incidentQuery.refetch();
    const latest = incidentForUser(refreshed.data?.incidents ?? [], user);
    if (!recoveryEligible(latest)) {
      setMessage("Lease 状态已经变化；Recovery 已停止，请刷新后重新检查。");
      return;
    }
    setConfirmationOpen(true);
    setConfirmation("");
    setSafeReason("");
    setPassword("");
  }

  const recovery = useMutation({
    mutationFn: async () => {
      if (!incident || confirmation.trim() !== incident.lease_id) {
        throw new ApiError(
          428,
          "LEASE_RECOVERY_CONFIRMATION_REQUIRED",
          "请输入准确 Lease ID 确认",
        );
      }
      if (!safeReason.trim()) {
        throw new ApiError(
          422,
          "LEASE_RECOVERY_REASON_REQUIRED",
          "请输入安全的 Recovery 原因",
        );
      }
      await reauthenticate(password);
      const refreshed = await incidentQuery.refetch();
      const latest = incidentForUser(refreshed.data?.incidents ?? [], user);
      if (!recoveryEligible(latest) || latest.lease_id !== incident.lease_id) {
        throw new ApiError(
          409,
          "LEASE_RECOVERY_STATE_CHANGED",
          "Lease 状态已经变化；未提交 Recovery",
        );
      }
      return retryLeaseRecycle(latest.lease_id, {
        confirmation: confirmation.trim(),
        safe_reason: safeReason.trim(),
      });
    },
    onSuccess: async (result) => {
      setOperation(result);
      setMessage(`Recovery Operation ${result.operation_id}：${result.status}`);
      closeConfirmation();
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["admin-lease-recovery-incidents"],
        }),
        queryClient.invalidateQueries({ queryKey: ["user", user.id] }),
        queryClient.invalidateQueries({ queryKey: ["users"] }),
        queryClient.invalidateQueries({ queryKey: ["operations"] }),
      ]);
    },
    onError: (reason) => {
      setPassword("");
      setMessage(
        reason instanceof ApiError
          ? `${reason.code}：${reason.message}`
          : "Recovery 未提交，请重新检查 Lease 状态。",
      );
    },
  });

  const renewalPolicy = useMutation({
    mutationFn: (approvalRequired: boolean) =>
      updateLeaseRenewalPolicy(user.id, approvalRequired),
    onSuccess: async (result) => {
      setPolicyMessage(
        result.policy.approval_required
          ? "该用户后续续期与恢复申请需要管理员审批。"
          : "该用户后续续期与恢复申请将在服务端校验后自动批准。",
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["user", user.id] }),
        queryClient.invalidateQueries({ queryKey: ["users"] }),
      ]);
    },
    onError: (reason) => {
      setPolicyMessage(
        reason instanceof ApiError
          ? `${reason.code}：${reason.message}`
          : "续期与恢复审批策略未更新。",
      );
    },
  });

  const approvalRequired = lifecycle?.renewal_approval_required ?? true;
  const policyPanel = (
    <section className="lease-renewal-policy" aria-label="续期与恢复审批策略">
      <div>
        <h3>续期与恢复审批策略</h3>
        <p className="muted">
          同时控制到期前续期和回收站恢复；只影响后续新申请，已有待审批申请保持不变。
        </p>
      </div>
      <label className="lease-renewal-policy-toggle">
        <input
          type="checkbox"
          checked={approvalRequired}
          disabled={!canManageRenewalPolicy || renewalPolicy.isPending}
          onChange={(event) => {
            setPolicyMessage(null);
            renewalPolicy.mutate(event.target.checked);
          }}
        />
        <span>续期与恢复需要管理员审批</span>
      </label>
      {!approvalRequired ? (
        <div className="notice" role="status">
          自动批准仍由后端执行时长上限、Lease 锁、资源所有权和安全策略校验。
        </div>
      ) : null}
      {policyMessage ? (
        <div
          className={renewalPolicy.isError ? "error-box" : "notice"}
          role={renewalPolicy.isError ? "alert" : "status"}
        >
          {policyMessage}
        </div>
      ) : null}
    </section>
  );

  if (!lifecycle?.has_lease && !incident) {
    return (
      <div className="lease-lifecycle-panel">
        {policyPanel}
        <div className="empty-plan">
          <div>
            <h2>尚无 Lease</h2>
            <p className="muted">当前真实资源模型没有返回计算租约。</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="lease-lifecycle-panel">
      <div className="detail-section-heading">
        <div>
          <h2>Lease / 生命周期</h2>
          <p className="muted">时间资格、数据库状态与异步回收状态分别显示。</p>
        </div>
        <div className="lifecycle-badges">
          <StatusBadge value={timeExpired ? "EXPIRED" : "ACTIVE"} />
          {incident?.recycle_state ? (
            <StatusBadge value={incident.recycle_state} />
          ) : null}
        </div>
      </div>

      {policyPanel}

      {anomaly ? (
        <div className="error-box lifecycle-critical" role="alert">
          <strong>Lease 生命周期异常</strong>
          <span>
            当前时间已超过 expires_at，但数据库状态仍为 ACTIVE；普通绿色 ACTIVE
            不代表租约仍有效。
          </span>
          {incident?.operation_status === "FAILED" ? (
            <span>回收失败，需要人工 Recovery。</span>
          ) : null}
        </div>
      ) : null}

      <dl className="kv-grid lifecycle-grid">
        <div className="kv">
          <dt>Lease ID</dt>
          <dd className="mono">{leaseId ?? "—"}</dd>
        </div>
        <div className="kv">
          <dt>Owner</dt>
          <dd>{leaseOwner ?? "—"}</dd>
        </div>
        <div className="kv">
          <dt>开始时间</dt>
          <dd>{localTime(startsAt)}</dd>
        </div>
        <div className="kv">
          <dt>到期时间</dt>
          <dd>{localTime(expiresAt)}</dd>
        </div>
        <div className="kv">
          <dt>时间状态</dt>
          <dd>{timeExpired ? "已过期" : "有效期内"}</dd>
        </div>
        <div className="kv">
          <dt>数据库状态</dt>
          <dd>{leaseState ?? "—"}</dd>
        </div>
        <div className="kv">
          <dt>回收状态</dt>
          <dd>{incident?.recycle_state ?? "—"}</dd>
        </div>
        <div className="kv">
          <dt>最后状态变更</dt>
          <dd>{localTime(incident?.last_transition_at)}</dd>
        </div>
        <div className="kv">
          <dt>Container runtime</dt>
          <dd>{incident?.container_observed_state ?? "—"}</dd>
        </div>
        <div className="kv">
          <dt>新连接授权</dt>
          <dd>{incident?.connection_authorization_state ?? "—"}</dd>
        </div>
        <div className="kv">
          <dt>Container SSH key</dt>
          <dd>{incident?.container_ssh_authorization_state ?? "—"}</dd>
        </div>
        <div className="kv">
          <dt>Recovery eligibility</dt>
          <dd>{canRecover ? "YES" : "NO"}</dd>
        </div>
      </dl>

      {incident ? (
        <section className="lifecycle-operation" aria-label="最近回收操作">
          <h3>最近回收操作</h3>
          <dl className="kv-grid">
            <div className="kv">
              <dt>Operation ID</dt>
              <dd className="mono">{incident.operation_id ?? "—"}</dd>
            </div>
            <div className="kv">
              <dt>Operation state</dt>
              <dd>{incident.operation_status ?? "—"}</dd>
            </div>
            <div className="kv">
              <dt>Error code</dt>
              <dd>{incident.error_code ?? "—"}</dd>
            </div>
            <div className="kv">
              <dt>Retry count</dt>
              <dd>{incident.attempt_count ?? "—（legacy evidence 未记录）"}</dd>
            </div>
            <div className="kv">
              <dt>Next retry</dt>
              <dd>{localTime(incident.next_retry_at)}</dd>
            </div>
            <div className="kv">
              <dt>Manual review</dt>
              <dd>{incident.manual_review_required ? "REQUIRED" : "NO"}</dd>
            </div>
          </dl>
          {incident.safe_error_message ? (
            <div className="notice" role="status">
              {incident.safe_error_message}
            </div>
          ) : null}
        </section>
      ) : isPlatformOwner && incidentQuery.isPending ? (
        <div className="muted">正在读取 Recovery 状态…</div>
      ) : isPlatformOwner && incidentQuery.isError ? (
        <div className="error-box" role="alert">
          Recovery 状态读取失败；不会显示或启用操作按钮。
        </div>
      ) : null}

      {operation ? (
        <section
          className="lifecycle-operation"
          aria-label="Recovery Operation 状态"
        >
          <h3>Recovery Operation</h3>
          <div className="plan-status-row">
            <StatusBadge value={operation.status} />
            <span className="mono">{operation.operation_id}</span>
          </div>
        </section>
      ) : null}

      {message ? (
        <div
          className={recovery.isError ? "error-box" : "notice"}
          role={recovery.isError ? "alert" : "status"}
        >
          {message}
        </div>
      ) : null}

      {canRecover ? (
        <div className="operation-actions">
          <Button
            tone="primary"
            type="button"
            disabled={incidentQuery.isFetching || recovery.isPending}
            onClick={() => void openConfirmation()}
          >
            重试回收
          </Button>
          <span className="muted">
            仅 platform_owner；提交前重新读取 Lease。
          </span>
        </div>
      ) : null}

      {confirmationOpen && incident ? (
        <div
          className="lease-recovery-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="lease-recovery-title"
        >
          <div className="lease-recovery-dialog-card">
            <div className="detail-section-heading">
              <div>
                <h2 id="lease-recovery-title">确认重试回收</h2>
                <p className="muted">这是高风险平台生命周期操作。</p>
              </div>
              <StatusBadge value="RECOVERY" />
            </div>
            <dl className="kv-grid">
              <div className="kv">
                <dt>User</dt>
                <dd>{incident.username}</dd>
              </div>
              <div className="kv">
                <dt>Lease ID</dt>
                <dd className="mono">{incident.lease_id}</dd>
              </div>
              <div className="kv">
                <dt>Lease expired</dt>
                <dd>YES</dd>
              </div>
              <div className="kv">
                <dt>Current state</dt>
                <dd>{incident.lease_state} / recycle failed</dd>
              </div>
              <div className="kv">
                <dt>Action</dt>
                <dd>Retry Recycle</dd>
              </div>
              <div className="kv">
                <dt>Expected</dt>
                <dd>RECYCLE_BIN / Container STOPPED</dd>
              </div>
            </dl>
            <div className="notice lifecycle-preservation-notice">
              此操作停止已过期开发容器、暂停 Container SSH
              授权并完成回收。用户数据、Quota、UID/GID、SSH public key record 与
              Slurm history 均保留；不会永久删除用户数据，也不会 Restore、Renew
              或 Reactivate。
            </div>
            <div className="form-field">
              <label htmlFor="lease-recovery-confirmation">
                输入完整 Lease ID 确认
              </label>
              <Input
                id="lease-recovery-confirmation"
                value={confirmation}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </div>
            <div className="form-field">
              <label htmlFor="lease-recovery-reason">Recovery 原因</label>
              <Input
                id="lease-recovery-reason"
                value={safeReason}
                maxLength={500}
                autoComplete="off"
                onChange={(event) => setSafeReason(event.target.value)}
              />
            </div>
            <div className="form-field">
              <label htmlFor="lease-recovery-password">
                platform_owner 密码（recent re-authentication）
              </label>
              <Input
                id="lease-recovery-password"
                type="password"
                value={password}
                autoComplete="current-password"
                onChange={(event) => setPassword(event.target.value)}
              />
              <span className="muted">
                密码只提交 Portal 正式认证接口，不写入 Operation、Audit
                或浏览器存储。
              </span>
            </div>
            <div className="operation-actions">
              <Button
                tone="primary"
                type="button"
                disabled={
                  recovery.isPending ||
                  confirmation.trim() !== incident.lease_id ||
                  !safeReason.trim() ||
                  !password
                }
                onClick={() => recovery.mutate()}
              >
                {recovery.isPending ? "正在安全校验…" : "确认重试回收"}
              </Button>
              <Button
                type="button"
                disabled={recovery.isPending}
                onClick={closeConfirmation}
              >
                取消
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
