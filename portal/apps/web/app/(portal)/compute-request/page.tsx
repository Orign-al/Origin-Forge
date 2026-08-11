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
  selfComputeRequest,
} from "../../../lib/api";
import { randomUuid } from "../../../lib/random-uuid";

const ACTIVE_STATES = [
  "REQUESTED",
  "UNDER_REVIEW",
  "APPROVED",
  "PROVISION_PLAN_READY",
];

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function ComputeRequestPage() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["self-compute-request"],
    queryFn: selfComputeRequest,
  });
  const [gpu, setGpu] = useState<0 | 1>(0);
  const [purpose, setPurpose] = useState("");
  const [note, setNote] = useState("");
  const create = useMutation({
    mutationFn: () =>
      createSelfComputeRequest({
        requested_gpu_max: gpu,
        requested_storage_bytes: 322122547200,
        requested_container_profile: "STANDARD_8CPU_32GB",
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
        <ErrorBlock message="无法读取你的计算资源申请。" />
      </>
    );
  }
  const current = query.data.request;
  const active = current && ACTIVE_STATES.includes(current.status);
  const approved =
    current && ["APPROVED", "PROVISION_PLAN_READY"].includes(current.status);

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
      {active && current ? (
        <Card className="detail-panel">
          <div className="detail-section-heading">
            <div>
              <h2>{approved ? "已批准，正在等待创建" : "等待管理员审批"}</h2>
              <p className="muted">申请 ID：{current.id}</p>
            </div>
            <StatusBadge value={current.status} />
          </div>
          <dl className="kv-grid">
            <div className="kv">
              <dt>GPU 最大额度</dt>
              <dd>{current.requested_gpu_max}</dd>
            </div>
            <div className="kv">
              <dt>Storage</dt>
              <dd>300GB STANDARD</dd>
            </div>
            <div className="kv">
              <dt>开发环境</dt>
              <dd>8 CPU / 32GB / GPU NONE</dd>
            </div>
            <div className="kv">
              <dt>首次 Lease</dt>
              <dd>4天（激活成功后开始）</dd>
            </div>
            <div className="kv">
              <dt>用途</dt>
              <dd>{current.purpose}</dd>
            </div>
            <div className="kv">
              <dt>提交时间</dt>
              <dd>{localTime(current.submitted_at)}</dd>
            </div>
          </dl>
          <div className="notice">
            当前尚未创建 Linux 用户、Container、Quota、Slurm Association、GPU
            Policy 或 Lease。
          </div>
          {current.status === "REQUESTED" ? (
            <div className="form-actions">
              <Button
                disabled={cancel.isPending}
                onClick={() => cancel.mutate(current.id)}
              >
                {cancel.isPending ? "撤回中…" : "撤回申请"}
              </Button>
            </div>
          ) : null}
          {cancel.isError ? (
            <div className="error-box" role="alert">
              {cancel.error instanceof ApiError
                ? cancel.error.message
                : "申请撤回失败"}
            </div>
          ) : null}
        </Card>
      ) : (
        <form onSubmit={submit}>
          {current?.status === "REJECTED" ? (
            <div className="notice" role="status">
              上次申请未通过：{current.review_note ?? "管理员未提供原因"}
              。你可以重新申请。
            </div>
          ) : null}
          <div className="section-grid">
            <SectionCard
              title="GPU需求"
              subtitle="表示 Slurm 最大额度，不独占 GPU"
            >
              <label className="form-field" htmlFor="compute-gpu">
                <span>GPU 最大数量</span>
                <select
                  id="compute-gpu"
                  className="ui-input"
                  value={gpu}
                  onChange={(event) =>
                    setGpu(event.target.value === "1" ? 1 : 0)
                  }
                >
                  <option value={0}>0</option>
                  <option value={1}>1</option>
                </select>
              </label>
              <p className="muted">
                GPU 任务通过 Portal 作业页面提交，最多 1 张。
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
                  <dd>NONE</dd>
                </div>
              </dl>
            </SectionCard>
          </div>
          <Card className="detail-panel">
            <div className="detail-section-heading">
              <div>
                <h2>申请说明</h2>
                <p className="muted">
                  首次资源有效期 4 天；到期前最后24小时可申请续期。
                </p>
              </div>
              <StatusBadge value="LEASE NOT STARTED" />
            </div>
            <div className="form-field">
              <label htmlFor="compute-purpose">
                用途 / Project Description
              </label>
              <textarea
                id="compute-purpose"
                className="ui-input identity-note"
                value={purpose}
                maxLength={1000}
                required
                onChange={(event) => setPurpose(event.target.value)}
                placeholder="例如：多用户平台验收"
              />
            </div>
            <div className="form-field">
              <label htmlFor="compute-note">备注（可选）</label>
              <textarea
                id="compute-note"
                className="ui-input identity-note"
                value={note}
                maxLength={1000}
                onChange={(event) => setNote(event.target.value)}
              />
            </div>
            <div className="notice">
              提交后进入管理员审批。批准和 dry-run
              均不会立即创建服务器资源，也不会提前启动 Lease。
            </div>
            {create.isError ? (
              <div className="error-box" role="alert">
                {create.error instanceof ApiError
                  ? create.error.message
                  : "计算资源申请提交失败"}
              </div>
            ) : null}
            <div className="form-actions">
              <Button
                tone="primary"
                type="submit"
                disabled={create.isPending || !purpose.trim()}
              >
                {create.isPending ? "提交中…" : "提交申请"}
              </Button>
            </div>
          </Card>
        </form>
      )}
    </>
  );
}
