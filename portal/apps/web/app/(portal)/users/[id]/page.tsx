"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button, Card, EmptyState, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../../components/PortalShell";
import { createOperation, userDetail } from "../../../../lib/api";
import {
  ObjectTable,
  type SimpleColumnDef,
  type SimpleRow,
} from "../../../../components/Tables";
import { SshKeyEnrollment } from "../../../../components/SshKeyEnrollment";

const TABS = [
  "概览",
  "登录安全",
  "计算资源",
  "Linux 身份",
  "GPU 隔离",
  "Slurm",
  "容器",
  "配额",
  "SSH 公钥",
  "操作记录",
  "审计日志",
] as const;

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

function asStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function Value({ children }: { children: unknown }) {
  if (children === null || children === undefined || children === "")
    return <span className="muted">—</span>;
  return <>{String(children)}</>;
}

export default function UserDetailPage() {
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const [selectedTab, setActiveTab] = useState<(typeof TABS)[number] | null>(
    null,
  );
  const activeTab =
    selectedTab ?? (searchParams.get("tab") === "ssh" ? "SSH 公钥" : "概览");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["user", params.id],
    queryFn: () => userDetail(params.id),
  });
  if (query.isPending)
    return (
      <>
        <PageHeading title="用户详情" />
        <LoadingBlock />
      </>
    );
  if (query.isError)
    return (
      <>
        <PageHeading title="用户详情" />
        <ErrorBlock />
      </>
    );
  const user = query.data.user;
  const linux = user.linux_identity ?? {};
  const compute = user.compute_onboarding;
  const plan = asRecord(compute?.plan);
  const hostAccess = asRecord(plan.proposed_host_access);
  const container = asRecord(plan.proposed_container);
  const gpuPolicy = asRecord(plan.proposed_gpu_policy);
  const validations = asRows(plan.validation_results);
  const conflicts = asRows(plan.conflicts);
  const isStaged = linux.onboarding_state === "STAGED";
  const isActive = linux.onboarding_state === "ACTIVE";
  const clientValidationPassed =
    linux.host_ssh_client_validation === "PASS" &&
    linux.container_ssh_client_validation === "PASS";
  const pilotAcceptancePassed = linux.pilot_acceptance_status === "PASSED";
  const hostSshPolicy = asRecord(linux.host_ssh_policy);
  const canManageSshKeys = ["STAGED", "ACTIVE"].includes(
    String(linux.onboarding_state ?? ""),
  );
  const sshKeyCount = Number(linux.ssh_key_count ?? 0);
  const activatePlanReady =
    asRecord(compute?.activate_dry_run?.plan).activate_status === "READY";
  async function createComputePlan() {
    setBusy(true);
    setMessage(null);
    try {
      await createOperation({
        operation_type: "user.plan",
        target_type: "compute_identity",
        target_id: "origin-pilot",
        request_summary:
          "为 Origin-al 规划独立 origin-pilot 计算身份（仅 dry-run）",
        payload: { username: "origin-pilot" },
        idempotency_key: `portal3a-origin-pilot-plan-${Date.now()}`,
      });
      setMessage(
        "DRAFT 与 Worker dry-run 已记录；没有创建 Linux 用户或任何宿主资源。",
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["user", params.id] }),
        queryClient.invalidateQueries({ queryKey: ["users"] }),
        queryClient.invalidateQueries({ queryKey: ["operations"] }),
      ]);
    } catch {
      setMessage("计划创建失败；宿主状态没有改变。");
    } finally {
      setBusy(false);
    }
  }
  const kv = (entries: Array<[string, unknown]>) => (
    <dl className="kv-grid">
      {entries.map(([label, value]) => (
        <div className="kv" key={label}>
          <dt>{label}</dt>
          <dd>
            <Value>{value}</Value>
          </dd>
        </div>
      ))}
    </dl>
  );
  return (
    <>
      <PageHeading
        title={user.display_name}
        description="网页身份、Linux 映射与计算资源状态严格分离"
        action={<StatusBadge value={user.account_state} />}
      />
      {user.normalized_login === "origin-al" ? (
        <div className="notice" style={{ marginBottom: 14 }}>
          当前 Origin-al 是平台恢复/管理账号，不会被转换为
          Pilot；独立计算身份建议为
          <strong> origin-pilot</strong>。
        </div>
      ) : null}
      {message ? (
        <div className="notice" role="status" style={{ marginBottom: 14 }}>
          {message}
        </div>
      ) : null}
      <div className="tabs" role="tablist" aria-label="用户详情分区">
        {TABS.map((tab) => (
          <button
            className={`tab ${activeTab === tab ? "tab-active" : ""}`}
            key={tab}
            onClick={() => setActiveTab(tab)}
            role="tab"
            aria-selected={activeTab === tab}
            type="button"
          >
            {tab}
          </button>
        ))}
      </div>
      <Card className="detail-panel">
        {activeTab === "概览"
          ? kv([
              ["网页登录名", user.login_name],
              ["规范化登录名", user.normalized_login],
              ["角色", user.roles.map((role) => role.name).join(", ")],
              ["账号状态", user.account_state],
              ["密码状态", user.password_state],
              ["计算 onboarding", user.resource_onboarding_state],
            ])
          : null}
        {activeTab === "登录安全" ? (
          <>
            {kv([
              ["密码状态", user.password_state],
              ["账号状态", user.account_state],
              ["激活时间", user.activated_at],
              ["最近登录", user.last_login_at],
            ])}
            {linux.unix_username === "origin-pilot"
              ? kv([
                  ["宿主 SSH 策略", hostSshPolicy.status],
                  [
                    "Public Key Authentication",
                    hostSshPolicy.pubkey_authentication === true
                      ? "ENABLED"
                      : "UNKNOWN",
                  ],
                  [
                    "Password Authentication",
                    hostSshPolicy.password_authentication === false
                      ? "DISABLED"
                      : "UNKNOWN",
                  ],
                  [
                    "Keyboard Interactive",
                    hostSshPolicy.keyboard_interactive_authentication === false
                      ? "DISABLED"
                      : "UNKNOWN",
                  ],
                  ["Required Authentication", "PUBLICKEY"],
                ])
              : null}
            {user.normalized_login === "origin-al" ? (
              <Link className="table-link" href="/account/security">
                打开当前账号安全设置
              </Link>
            ) : (
              <div className="notice">管理员会话撤销需要单独审批。</div>
            )}
          </>
        ) : null}
        {activeTab === "计算资源" ? (
          user.normalized_login === "origin-al" ? (
            <div className="compute-plan">
              <div className="detail-section-heading">
                <div>
                  <h2>独立计算身份 Onboarding</h2>
                  <p className="muted">
                    {isActive
                      ? clientValidationPassed
                        ? "管理映射 origin-al 保持不变；独立计算身份已 ACTIVE，两项真实客户端 SSH 验证已确认。"
                        : "管理映射 origin-al 保持不变；独立计算身份已 ACTIVE，等待用户客户端 SSH 验证。"
                      : isStaged
                        ? "管理映射 origin-al 保持不变；独立计算身份已安全 Stage，尚未激活登录。"
                        : "管理映射 origin-al 保持不变；这里只创建数据库 DRAFT 和只读 dry-run。"}
                  </p>
                </div>
                <StatusBadge
                  value={compute?.draft_state ?? "DRAFT NOT CREATED"}
                />
              </div>
              {compute?.status === "ACTIVE" ? (
                <>
                  <div className="plan-status-row">
                    <StatusBadge value="ACTIVE" />
                    <span className="mono">
                      Operation {compute.operation_id}
                    </span>
                    <span>
                      Client Validation {clientValidationPassed ? "PASS" : "PENDING"}
                    </span>
                  </div>
                  {kv([
                    ["Portal owner", "Origin-al"],
                    ["管理 Linux 映射", "origin-al（保持不变）"],
                    ["计算身份", linux.unix_username],
                    ["UID/GID", `${String(linux.uid)}/${String(linux.gid)}`],
                    ["Shell", linux.shell],
                    ["Linux 密码", linux.password_state],
                    ["authorized_keys", linux.authorized_keys_state],
                    ["宿主访问", linux.host_access_state],
                    ["SSH 公钥", linux.ssh_key_state],
                    [
                      "宿主 SSH Client Validation",
                      linux.host_ssh_client_validation,
                    ],
                    [
                      "容器 SSH Client Validation",
                      linux.container_ssh_client_validation,
                    ],
                    ["GPU 隔离", linux.gpu_isolation_state],
                    ["作业外 GPU", "DENIED"],
                    [
                      "Slurm",
                      `${String(linux.slurm_account)} / ${String(linux.slurm_qos)} / ${String(linux.max_gpus)} GPU`,
                    ],
                    ["Node", linux.slurm_node_state],
                    ["Queue", linux.slurm_queue],
                    ["配额", "300GB hard limit"],
                    [
                      "容器",
                      `${String(linux.container_name)} / ${String(linux.container_state)} / GPU ${String(linux.container_gpu)}`,
                    ],
                    ["Guard", linux.guard_state],
                    ["Pilot 验收", linux.pilot_acceptance_status],
                    ["CPU Job ID", linux.pilot_cpu_job_id],
                    ["单 GPU Job ID", linux.pilot_gpu_job_id],
                    ["Allocated GPU UUID", linux.pilot_allocated_gpu_uuid],
                  ])}
                  <div className="operation-actions">
                    <Link className="table-link" href="/access">
                      打开连接页面
                    </Link>
                  </div>
                  <div className="notice">
                    ACCOUNT ACTIVE；SCHEDULER CURRENTLY DRAINED。
                    {clientValidationPassed
                      ? pilotAcceptancePassed
                        ? "两项真实客户端 SSH 验证与首个 CPU/单 GPU Pilot 验收均已通过；节点仍等待最终上线审批。"
                        : "两项真实客户端 SSH 验证已通过；节点仍等待 Pilot 验收。"
                      : "宿主与容器服务端已就绪，真实用户必须使用自己保存的私钥完成两项客户端验证。"}
                  </div>
                </>
              ) : compute?.status === "STAGED" ? (
                <>
                  <div className="plan-status-row">
                    <StatusBadge value="STAGED" />
                    <span className="mono">
                      Operation {compute.operation_id}
                    </span>
                    <span>宿主登录未激活</span>
                  </div>
                  {kv([
                    ["Portal owner", "Origin-al"],
                    ["管理 Linux 映射", "origin-al（保持不变）"],
                    ["计算身份", linux.unix_username],
                    ["UID/GID", `${String(linux.uid)}/${String(linux.gid)}`],
                    ["Shell", linux.shell],
                    ["Linux 密码", linux.password_state],
                    ["authorized_keys", linux.authorized_keys_state],
                    ["宿主登录", "未激活"],
                    [
                      "SSH 公钥",
                      sshKeyCount > 0 ? "已验证，待安装" : "激活前必填",
                    ],
                    ["GPU 隔离", linux.gpu_isolation_state],
                    ["作业外 GPU open", linux.gpu_open_state],
                    ["作业外 CUDA context", linux.cuda_context_state],
                    [
                      "Slurm",
                      `${String(linux.slurm_account)} / ${String(linux.slurm_qos)} / ${String(linux.max_gpus)} GPU`,
                    ],
                    ["配额", "300GB hard limit"],
                    [
                      "容器",
                      `${String(linux.container_name)} / ${String(linux.container_state)} / GPU ${String(linux.container_gpu)}`,
                    ],
                    ["Guard", linux.guard_state],
                  ])}
                  <div className="operation-actions">
                    <Button
                      type="button"
                      onClick={() => setActiveTab("SSH 公钥")}
                    >
                      设置 SSH 公钥
                    </Button>
                    <Button disabled>Activate（未审批）</Button>
                  </div>
                  <div className="notice">
                    {sshKeyCount === 0
                      ? "SSH PUBLIC KEY REQUIRED。"
                      : activatePlanReady
                        ? "SSH 公钥已验证，Activate dry-run 已通过；仍需管理员明确审批。"
                        : "SSH 公钥已验证，等待生成 Activate dry-run。"}
                    当前没有 authorized_keys，Shell 仍为
                    /usr/sbin/nologin，容器保持 STOPPED。
                  </div>
                </>
              ) : compute?.status !== "DRAFT" || !Object.keys(plan).length ? (
                <div className="empty-plan">
                  <EmptyState
                    title="DRAFT NOT CREATED"
                    detail="建议账号 origin-pilot；创建计划不会预留 UID、project ID 或端口"
                  />
                  <Button
                    tone="primary"
                    onClick={() => void createComputePlan()}
                    disabled={busy}
                  >
                    创建计算资源草稿
                  </Button>
                </div>
              ) : (
                <>
                  <div className="plan-status-row">
                    <StatusBadge
                      value={String(plan.plan_status ?? "UNKNOWN")}
                    />
                    <span className="mono">
                      Operation {compute.operation_id}
                    </span>
                    <span>execution_enabled=false</span>
                  </div>
                  {kv([
                    ["Portal owner", plan.portal_owner],
                    ["管理 Linux 映射", "origin-al（保持不变）"],
                    ["建议 Unix username", plan.proposed_username],
                    [
                      "Host access",
                      hostAccess.enabled === true
                        ? "enabled（Stage 后仍 nologin）"
                        : "—",
                    ],
                    ["计划状态", plan.proposal_state],
                    [
                      "UID",
                      `${String(plan.proposed_uid ?? "—")} — NOT RESERVED`,
                    ],
                    [
                      "GID",
                      `${String(plan.proposed_gid ?? "—")} — NOT RESERVED`,
                    ],
                    [
                      "Project ID",
                      `${String(plan.proposed_project_id ?? "—")} — NOT RESERVED`,
                    ],
                    [
                      "Quota",
                      `${String(plan.proposed_quota_hard_limit_gb ?? "—")} GB hard`,
                    ],
                    [
                      "Slurm",
                      `${String(plan.proposed_slurm_account ?? "—")} / ${String(plan.proposed_qos ?? "—")}`,
                    ],
                    ["最大 GPU", plan.proposed_max_gpus],
                    [
                      "SSH 端口",
                      `${String(plan.proposed_ssh_port ?? "—")} @ ${String(container.network_bind ?? "—")} — NOT RESERVED`,
                    ],
                    [
                      "SSH 公钥契约",
                      plan.stage_ssh_key_status ??
                        plan.ssh_key_status ??
                        compute?.ssh_key_status ??
                        "NOT_REQUIRED_FOR_STAGE",
                    ],
                  ])}
                  <h3 className="subheading">GPU 隔离计划</h3>
                  {kv([
                    ["方法", gpuPolicy.method],
                    ["Unit", gpuPolicy.unit],
                    ["Drop-in", gpuPolicy.dropin_path],
                    ["DevicePolicy", gpuPolicy.device_policy],
                    ["DeviceAllow", "无"],
                    ["全局 user.slice", "不修改"],
                    ["user-.slice", "不修改"],
                  ])}
                  <h3 className="subheading">长期容器计划</h3>
                  {kv([
                    ["容器名", container.name],
                    ["CPU", container.cpus],
                    ["内存", `${String(container.memory_gb ?? "—")} GB`],
                    ["PIDs", container.pids_limit],
                    ["GPU", container.gpu],
                  ])}
                  <h3 className="subheading">冲突与验证</h3>
                  {conflicts.length ? (
                    <div className="error-box">
                      {conflicts.map((item, index) => (
                        <div key={`${String(item.code)}-${index}`}>
                          {String(item.code)}：{String(item.message)}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="notice">
                      未发现资源冲突；所有候选值仍为 PROPOSED — NOT RESERVED。
                    </div>
                  )}
                  <ObjectTable
                    rows={validations}
                    columns={VALIDATION_COLUMNS}
                  />
                  <div className="plan-step-grid">
                    {[
                      ["Stage 计划", asStrings(plan.stage_steps)],
                      ["Activate 计划", asStrings(plan.activate_steps)],
                      ["回滚计划", asStrings(plan.rollback_steps)],
                    ].map(([title, steps]) => (
                      <section key={String(title)}>
                        <h3>{String(title)}</h3>
                        <ol>
                          {(steps as string[]).map((step) => (
                            <li key={step}>{step}</li>
                          ))}
                        </ol>
                      </section>
                    ))}
                  </div>
                  <div className="operation-actions">
                    <Button
                      onClick={() => void createComputePlan()}
                      disabled={busy}
                    >
                      重新执行 dry-run
                    </Button>
                    <Button disabled aria-label="Stage 未授权">
                      Stage（无需公钥）未授权
                    </Button>
                    <Button disabled aria-label="Activate 未授权">
                      Activate（需公钥）未授权
                    </Button>
                  </div>
                  <div className="notice">
                    Stage 不读取、不验证、不安装 SSH 公钥；Stage 后状态为
                    REQUIRED_BEFORE_ACTIVATION。Activate
                    才要求受控公钥记录，且下一阶段 批准前 Worker
                    不会进入真实写模式。
                  </div>
                </>
              )}
            </div>
          ) : (
            <EmptyState
              title="没有可用的计算身份计划"
              detail="当前账号不属于 Portal-3A 目标"
            />
          )
        ) : null}
        {activeTab === "Linux 身份"
          ? kv([
              ["Linux 用户", linux.unix_username ?? user.unix_username],
              ["UID", linux.uid],
              ["GID", linux.gid],
              ["Shell", linux.shell],
              ["密码状态", linux.password_state],
              ["authorized_keys", linux.authorized_keys_state],
              ["宿主访问", linux.host_access_state],
              ["资源状态", linux.onboarding_state],
            ])
          : null}
        {activeTab === "GPU 隔离"
          ? kv([
              ["隔离状态", linux.gpu_isolation_state ?? "NOT_APPLIED"],
              [
                "方法",
                linux.uid ? `user-${String(linux.uid)}.slice` : "NOT_APPLIED",
              ],
              ["宿主直接 GPU", linux.uid ? "DENIED" : "NOT_ENROLLED"],
              ["GPU open probe", linux.gpu_open_state],
              ["CUDA context probe", linux.cuda_context_state],
              ["Guard", linux.guard_state],
            ])
          : null}
        {activeTab === "Slurm"
          ? kv([
              ["Account", linux.slurm_account],
              ["QOS", linux.slurm_qos],
              ["最大 GPU", linux.max_gpus],
              ["状态", user.resource_onboarding_state],
            ])
          : null}
        {activeTab === "容器"
          ? kv([
              ["容器", linux.container_name],
              ["SSH 端口", linux.container_port],
              ["GPU", linux.container_name ? "无" : "NOT_ENROLLED"],
              ["状态", linux.container_state],
              ["CPU", linux.container_cpus],
              [
                "内存",
                linux.container_memory_gb
                  ? `${String(linux.container_memory_gb)} GB`
                  : null,
              ],
              ["PIDs", linux.container_pids_limit],
              ["镜像 digest", linux.container_image_digest],
            ])
          : null}
        {activeTab === "配额"
          ? kv([
              ["Project ID", linux.project_id],
              ["Quota bytes", linux.quota_bytes],
              ["Hard limit", linux.quota_bytes ? "300GB" : null],
            ])
          : null}
        {activeTab === "SSH 公钥" ? (
          canManageSshKeys ? (
            <SshKeyEnrollment
              userId={user.id}
              username={String(linux.unix_username ?? "origin-pilot")}
              computeState={String(linux.onboarding_state ?? "UNKNOWN")}
              managedUserId={String(linux.managed_user_id ?? "") || null}
              activateDryRun={
                compute?.activate_dry_run?.plan
                  ? asRecord(compute.activate_dry_run.plan)
                  : null
              }
            />
          ) : (
            <EmptyState
              title="等待计算身份 Stage"
              detail="Stage 完成后才能生成或导入 SSH 公钥。"
            />
          )
        ) : null}
        {activeTab === "操作记录" ? (
          <EmptyState
            title="请在审批任务页按用户筛选"
            detail="本页不伪造资源操作记录"
          />
        ) : null}
        {activeTab === "审计日志" ? (
          <EmptyState
            title="请在审计页按对象筛选"
            detail="审计不包含密码、token 或 session"
          />
        ) : null}
      </Card>
      <div className="section-grid">
        <SectionCard title="身份边界">
          网页密码与 Linux/SSH 密码完全分离；计算 onboarding
          不会因网页账号激活而自动执行。
        </SectionCard>
        <SectionCard title="当前阶段">
          {isActive
            ? clientValidationPassed
              ? pilotAcceptancePassed
                ? "origin-pilot 已 ACTIVE；Host/Container SSH Client Validation PASS；首个 CPU/单 GPU Pyxis Pilot 验收 PASSED；Slurm 保持 DRAIN，等待最终上线审批。"
                : "origin-pilot 已 ACTIVE；Host/Container SSH Client Validation PASS；容器 RUNNING/GPU NONE，Slurm 保持 DRAIN。"
              : "origin-pilot 已 ACTIVE；Host/Container SSH 服务端 READY，客户端验证 PENDING；容器 RUNNING/GPU NONE，Slurm 保持 DRAIN。"
            : isStaged
              ? sshKeyCount > 0
                ? "origin-pilot 已 STAGED；SSH 公钥已验证但未安装，登录、容器启动与 Activate 均未执行，Slurm 保持 DRAIN。"
                : "origin-pilot 已 STAGED；SSH 公钥尚未登记，登录、容器启动与 Activate 均未执行，Slurm 保持 DRAIN。"
              : "所有资源写操作只形成审批任务和 Worker dry-run，Slurm 保持 DRAIN。"}
        </SectionCard>
      </div>
    </>
  );
}
