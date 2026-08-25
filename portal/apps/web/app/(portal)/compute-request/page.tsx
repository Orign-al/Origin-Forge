"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { Button, Card, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../components/PortalShell";
import {
  ApiError,
  cancelSelfComputeRequest,
  createSelfComputeRequest,
  type DevelopmentContainerProfile,
  selfComputeRequest,
} from "../../../lib/api";
import { randomUuid } from "../../../lib/random-uuid";
import { useI18n, type Locale } from "../../../lib/i18n";

const ACTIVE_STATES = [
  "REQUESTED",
  "UNDER_REVIEW",
  "APPROVED",
  "RETRY_AUTHORIZED",
  "PROVISION_PLAN_READY",
  "PROVISIONING",
];

function localTime(value: string | null | undefined, locale: Locale) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(locale, {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function ComputeRequestPage() {
  const queryClient = useQueryClient();
  const { locale, t } = useI18n();
  const query = useQuery({
    queryKey: ["self-compute-request"],
    queryFn: selfComputeRequest,
  });
  const [gpu, setGpu] = useState<0 | 1>(0);
  const [profile, setProfile] =
    useState<DevelopmentContainerProfile>("STANDARD_8CPU_32GB");
  const [purpose, setPurpose] = useState("");
  const [note, setNote] = useState("");
  const create = useMutation({
    mutationFn: () =>
      createSelfComputeRequest({
        requested_gpu_max: profile === "GPU_1_8CPU_32GB" ? 1 : gpu,
        requested_storage_bytes: 322122547200,
        requested_container_profile: profile,
        requested_lease_seconds: 345600,
        purpose,
        user_note: note.trim() || null,
        idempotency_key: randomUuid(),
      }),
    onSuccess: async () => {
      setPurpose("");
      setNote("");
      await queryClient.invalidateQueries({
        queryKey: ["self-compute-request"],
      });
    },
  });
  const cancel = useMutation({
    mutationFn: (id: string) => cancelSelfComputeRequest(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["self-compute-request"],
      });
    },
  });

  if (query.isPending) {
    return (
      <>
        <PageHeading title="申请计算资源" />
        <LoadingBlock />
      </>
    );
  }
  if (query.isError) {
    return (
      <>
        <PageHeading title="申请计算资源" />
        <ErrorBlock message={t("无法读取你的计算资源申请。")} />
      </>
    );
  }
  const current = query.data.request;
  const active = current && ACTIVE_STATES.includes(current.status);
  const failed = current?.status === "FAILED";
  const approved =
    current &&
    [
      "APPROVED",
      "RETRY_AUTHORIZED",
      "PROVISION_PLAN_READY",
      "PROVISIONING",
    ].includes(current.status);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (purpose.trim()) create.mutate();
  }

  return (
    <>
      <PageHeading
        title="申请计算资源"
        description="申请标准开发环境；资源只在后续管理员 Gate 获批后创建"
        action={<StatusBadge value={current?.status ?? "NOT REQUESTED"} />}
      />
      {(active || failed) && current ? (
        <Card className="detail-panel">
          <div className="detail-section-heading">
            <div>
              <h2>
                {failed
                  ? t("计算环境创建失败，管理员处理中")
                  : approved
                    ? t("已批准，正在等待创建")
                    : t("等待管理员审批")}
              </h2>
              <p className="muted">{t("申请 ID：{id}", { id: current.id })}</p>
            </div>
            <StatusBadge value={current.status} />
          </div>
          <dl className="kv-grid">
            <div className="kv">
              <dt>{t("GPU 最大额度")}</dt>
              <dd>{current.requested_gpu_max}</dd>
            </div>
            <div className="kv">
              <dt>Storage</dt>
              <dd>300GB STANDARD</dd>
            </div>
            <div className="kv">
              <dt>{t("开发环境")}</dt>
              <dd>
                8 CPU / 32GB / GPU{" "}
                {current.requested_container_profile === "GPU_1_8CPU_32GB"
                  ? "1 (Slurm scheduled)"
                  : "NONE"}
              </dd>
            </div>
            <div className="kv">
              <dt>{t("首次 Lease")}</dt>
              <dd>{t("4天（激活成功后开始）")}</dd>
            </div>
            <div className="kv">
              <dt>{t("用途")}</dt>
              <dd>{current.purpose}</dd>
            </div>
            <div className="kv">
              <dt>{t("提交时间")}</dt>
              <dd>{localTime(current.submitted_at, locale)}</dd>
            </div>
          </dl>
          <div className="notice">
            {failed
              ? t(
                  current.user_status_message ??
                    "计算环境创建失败，平台管理员正在处理。你的申请仍被保留，无需重新提交。",
                )
              : t(
                  "当前尚未创建 Linux 用户、Container、Quota、Slurm Association、GPU Policy 或 Lease。",
                )}
          </div>
          {current.status === "REQUESTED" ? (
            <div className="form-actions">
              <Button
                disabled={cancel.isPending}
                onClick={() => cancel.mutate(current.id)}
              >
                {cancel.isPending ? t("撤回中…") : t("撤回申请")}
              </Button>
            </div>
          ) : null}
          {cancel.isError ? (
            <div className="error-box" role="alert">
              {cancel.error instanceof ApiError
                ? t(cancel.error.message)
                : t("申请撤回失败")}
            </div>
          ) : null}
        </Card>
      ) : (
        <form onSubmit={submit}>
          {current?.status === "REJECTED" ? (
            <div className="notice" role="status">
              {t("上次申请未通过：{reason}。你可以重新申请。", {
                reason: current.review_note ?? t("管理员未提供原因"),
              })}
            </div>
          ) : null}
          <div className="section-grid">
            <SectionCard title="开发容器 Profile" subtitle="CPU 为默认配置">
              <label className="form-field" htmlFor="development-profile">
                <span>Profile</span>
                <select
                  id="development-profile"
                  className="ui-input"
                  value={profile}
                  onChange={(event) => {
                    const next = event.target
                      .value as DevelopmentContainerProfile;
                    setProfile(next);
                    if (next === "GPU_1_8CPU_32GB") setGpu(1);
                  }}
                >
                  <option value="STANDARD_8CPU_32GB">CPU Development</option>
                  <option value="GPU_1_8CPU_32GB">GPU Development</option>
                </select>
              </label>
              <p className="muted">
                GPU Development 通过 Slurm 分配 1 张 H100，仅容器运行期间持有。
              </p>
            </SectionCard>
            <SectionCard
              title="GPU需求"
              subtitle="Slurm 总额度；GPU Profile 固定为 1"
            >
              <label className="form-field" htmlFor="compute-gpu">
                <span>{t("GPU 最大数量")}</span>
                <select
                  id="compute-gpu"
                  className="ui-input"
                  value={profile === "GPU_1_8CPU_32GB" ? 1 : gpu}
                  disabled={profile === "GPU_1_8CPU_32GB"}
                  onChange={(event) =>
                    setGpu(event.target.value === "1" ? 1 : 0)
                  }
                >
                  <option value={0}>0</option>
                  <option value={1}>1</option>
                </select>
              </label>
              <p className="muted">
                {profile === "GPU_1_8CPU_32GB"
                  ? t(
                      "GPU Development 占用该用户唯一 GPU 额度；停止容器后额度返回调度器。",
                    )
                  : t("GPU 任务通过 Portal 作业页面提交，最多 1 张。")}
              </p>
            </SectionCard>
            <SectionCard title="标准开发环境" subtitle="规格不可由普通用户修改">
              <dl className="kv-grid">
                <div className="kv">
                  <dt>CPU</dt>
                  <dd>8</dd>
                </div>
                <div className="kv">
                  <dt>Memory</dt>
                  <dd>32GB</dd>
                </div>
                <div className="kv">
                  <dt>Storage</dt>
                  <dd>300GB</dd>
                </div>
                <div className="kv">
                  <dt>Container GPU</dt>
                  <dd>
                    {profile === "GPU_1_8CPU_32GB"
                      ? "1 (Slurm scheduled)"
                      : "NONE"}
                  </dd>
                </div>
              </dl>
            </SectionCard>
          </div>
          <Card className="detail-panel">
            <div className="detail-section-heading">
              <div>
                <h2>{t("申请说明")}</h2>
                <p className="muted">
                  {t("首次资源有效期 4 天；到期前最后24小时可申请续期。")}
                </p>
              </div>
              <StatusBadge value="LEASE NOT STARTED" />
            </div>
            <div className="form-field">
              <label htmlFor="compute-purpose">
                {t("用途 / Project Description")}
              </label>
              <textarea
                id="compute-purpose"
                className="ui-input identity-note"
                value={purpose}
                maxLength={1000}
                required
                onChange={(event) => setPurpose(event.target.value)}
                placeholder={t("例如：多用户平台验收")}
              />
            </div>
            <div className="form-field">
              <label htmlFor="compute-note">{t("备注（可选）")}</label>
              <textarea
                id="compute-note"
                className="ui-input identity-note"
                value={note}
                maxLength={1000}
                onChange={(event) => setNote(event.target.value)}
              />
            </div>
            <div className="notice">
              {t(
                "提交后进入管理员审批。管理员批准一次后，系统自动完成安全检查和环境创建； Lease 仍只会在你登记 Container 公钥并 Activate 后开始。",
              )}
            </div>
            {create.isError ? (
              <div className="error-box" role="alert">
                {create.error instanceof ApiError
                  ? t(create.error.message)
                  : t("计算资源申请提交失败")}
              </div>
            ) : null}
            <div className="form-actions">
              <Button
                tone="primary"
                type="submit"
                disabled={create.isPending || !purpose.trim()}
              >
                {create.isPending ? t("提交中…") : t("提交申请")}
              </Button>
            </div>
          </Card>
        </form>
      )}
    </>
  );
}
