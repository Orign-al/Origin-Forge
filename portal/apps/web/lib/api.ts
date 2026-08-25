import { ApiError, apiFetch } from "@h100-portal/api-client";

import { randomUuid } from "./random-uuid";

export { ApiError };

export type ComputeLifecycleSummary = {
  has_lease: boolean;
  lease_id?: string;
  owner?: string;
  starts_at?: string;
  expires_at?: string;
  time_expired?: boolean;
  lease_state?: string;
};

export type AdminLeaseRecoveryIncident = {
  lease_id: string;
  portal_user_id: string;
  username: string;
  owner: string;
  starts_at: string;
  expires_at: string;
  current_time: string;
  time_expired: boolean;
  lease_state: string;
  recycle_state: string | null;
  last_transition_at: string | null;
  compute_environment_state: string;
  container_name: string;
  container_desired_state: string;
  container_observed_state: string;
  connection_authorization_state: string;
  container_ssh_authorization_state: string;
  operation_id: string | null;
  operation_type: string | null;
  operation_status: string | null;
  operation_started_at: string | null;
  operation_finished_at: string | null;
  error_code: string | null;
  safe_error_message: string | null;
  attempt_count: number | null;
  next_retry_at: string | null;
  manual_review_required: boolean;
  recovery_available: boolean;
  data_delete_allowed: false;
};

export type LeaseRecoveryOperationResult = {
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  operation_id: string;
  lease_id: string;
  idempotent_replay: boolean;
};

export type User = {
  id: string;
  login_name: string;
  normalized_login: string;
  display_name: string;
  note?: string | null;
  unix_username: string | null;
  account_state: string;
  password_state: string;
  resource_onboarding_state: string;
  created_at?: string;
  activated_at?: string | null;
  last_login_at?: string | null;
  failed_login_count?: number;
  locked_until?: string | null;
  roles: Array<{ name: string; description: string }>;
  password_actions?: PasswordActionMetadata[];
  compute_request?: {
    id: string;
    status: string;
    gpu_max: 0 | 1;
    submitted_at: string | null;
  } | null;
  compute_lifecycle?: ComputeLifecycleSummary;
  linux_identity?: Record<string, unknown>;
  compute_onboarding?: {
    status: string;
    compute_username: string | null;
    draft_state: string;
    operation_id?: string;
    operation_status?: string;
    operation_type?: string;
    ssh_key_status?: string;
    plan?: Record<string, unknown> | null;
    result_summary?: string | null;
    error_code?: string | null;
    activate_dry_run?: {
      operation_id: string;
      status: string;
      plan?: Record<string, unknown> | null;
      result_summary?: string | null;
      error_code?: string | null;
    } | null;
  };
};

export type PasswordActionMetadata = {
  id: string;
  purpose: "INITIAL_PASSWORD_SETUP" | "PASSWORD_RESET";
  state: "ACTIVE" | "USED" | "REVOKED" | "EXPIRED";
  created_at: string;
  expires_at: string;
  used_at: string | null;
  revoked_at: string | null;
  created_by: string | null;
};

export type GeneratedPasswordActionLink = {
  status: "GENERATED";
  purpose: "INITIAL_PASSWORD_SETUP" | "PASSWORD_RESET";
  setup_url: string;
  expires_at: string;
  token: PasswordActionMetadata;
  operation_id: string;
};

export type SshEnrollment = {
  required: boolean;
  managed_user_id: string | null;
  compute_identity: string | null;
  compute_state: string;
  validated_key_count: number;
  ssh_key_state: string;
  setup_path: string | null;
};

export type SshKeyRecord = {
  id: string;
  managed_user_id: string;
  key_type: string;
  fingerprint_sha256: string;
  comment: string;
  scope: "HOST" | "CONTAINER" | "BOTH";
  state: "VALIDATED" | "INSTALLED" | "REVOKED";
  generation_method: "BROWSER_GENERATED" | "IMPORTED";
  created_at: string;
  created_by: string;
  validated_at: string | null;
  installed_at: string | null;
  revoked_at: string | null;
};

export type SshKeyList = {
  status: string;
  keys: SshKeyRecord[];
  count: number;
  maximum_active_keys: number;
  enrollment: SshEnrollment;
};

export type SelfComputeActivationResult =
  | {
      status: "ACTIVATING";
      operation_id: string;
      idempotent_replay: true;
      environment_state: "STAGED";
      container_state: "ACTIVATING";
      ssh_state: "INSTALLING";
      lease: null;
    }
  | {
      status: "ACTIVE" | "ALREADY_ACTIVE";
      operation_id?: string;
      idempotent_replay: boolean;
      environment_state: "ACTIVE";
      container_state: "RUNNING";
      ssh_state: "READY";
      lease: {
        id: string;
        state: string;
        starts_at: string;
        expires_at: string;
        duration_seconds: 345600;
      };
    };

export type Overview = {
  status: string;
  platform: {
    status: string;
    node?: Record<string, unknown>;
    jobs?: Record<string, unknown>;
    systemd?: Record<string, unknown>;
    gpu?: {
      status: string;
      count?: number;
      gpus?: Array<Record<string, string>>;
    };
    gpu_isolation?: Record<string, unknown>;
    error?: Record<string, unknown>;
  };
  containers: {
    status: string;
    count?: number;
    containers?: Array<Record<string, unknown>>;
    error?: Record<string, unknown>;
  };
  storage: {
    status: string;
    mounts?: Array<Record<string, string>>;
    volume_groups?: Record<string, unknown>;
    error?: Record<string, unknown>;
  };
  registries: {
    status: string;
    registries?: Array<Record<string, string>>;
    error?: Record<string, unknown>;
  };
  alerts: {
    status: string;
    count?: number;
    alerts?: Array<Record<string, unknown>>;
    error?: Record<string, unknown>;
  };
  monitoring?: Record<string, unknown>;
  gpu_health?: Record<string, unknown>;
  recent_audit?: Array<Record<string, unknown>>;
  identity: {
    status: string;
    portal_users: number;
    managed_linux_users: number;
    account_states: Record<string, number>;
    onboarding_states: Record<string, number>;
  };
  tasks: {
    status: string;
    pending_approval: number;
    failed: number;
    total: number;
  };
  production_pilot: {
    status: string;
    state: string;
    mode: string;
    managed_users: number;
    active_managed_user: string;
    node_name: string;
    node_state: string;
    scheduler: string;
    queue: string;
    gpu_capacity: number;
    per_user_max_gpu: number;
    started_at?: string;
  };
};

export type PortalSession = {
  id: string;
  created_at: string;
  last_seen_at: string;
  idle_expires_at: string;
  absolute_expires_at: string;
  current: boolean;
  source_ip: string;
};

export type ComputeLease = {
  id?: string;
  state: string;
  active: boolean;
  starts_at?: string;
  expires_at?: string;
  remaining_seconds?: number;
  renewal_available: boolean;
  renewal_available_from?: string;
  gpu_count?: number;
  max_duration_seconds: number;
  renewal_window_seconds: number;
  auto_renew: false;
  restore_required?: boolean;
  pending_renewal_id?: string | null;
};

export type SelfEnvironment = {
  state: string;
  gpu_max: number;
  host_access: "DISABLED";
  job_submission: string;
  lease: ComputeLease;
  container: {
    id: string;
    name: string;
    state: string;
    connection_state: string;
    profile: DevelopmentContainerProfile;
    gpu: 0 | 1;
    gpu_allocation_state: "NONE" | "ALLOCATED";
    cpus: number;
    memory_gb: number;
    pids_limit: number;
  };
  storage: {
    quota_bytes: number | null;
    state: string;
    workspace: string;
    container_mount: "/workspace";
    default_job_workdir: string;
  };
};

export type SelfJob = {
  id: string;
  slurm_job_id: number | null;
  name: string;
  state: string;
  cpus: number;
  memory_mb: number;
  gpu_count: 0 | 1;
  time_limit_seconds: number;
  script_path: string;
  workdir: string;
  stdout_path: string;
  stderr_path: string;
  lease_deadline_at: string;
  created_at: string;
  submitted_at: string | null;
  finished_at: string | null;
  exit_code: string | null;
};

export type SelfTerminal = {
  id: string;
  operation_id: string;
  container: string;
  username: string;
  gpu: "NONE" | "SLURM_ALLOCATED_1";
  host_access: "DISABLED";
  state: string;
  expires_at: string;
  idle_timeout_seconds: number;
  max_duration_seconds: number;
};

export type DevelopmentContainerProfile =
  "STANDARD_8CPU_32GB" | "GPU_1_8CPU_32GB";

export type ProvisionPlan = {
  id: string;
  attempt_number: number;
  attempt_reason: "INITIAL" | "RESERVATION_EXPIRED" | "STAGE_RETRY";
  state: string;
  username?: string;
  uid?: number;
  gid?: number;
  project_id?: number;
  container_name?: string;
  container_ssh_port?: number;
  storage_bytes: number;
  container_profile: DevelopmentContainerProfile;
  container_cpus: 8;
  container_memory_gb: 32;
  container_pids_limit: 4096;
  container_gpu: 0 | 1;
  slurm_account?: "company";
  slurm_qos?: "general";
  gpu_max: 0 | 1;
  lease_seconds: 345600;
  lease_state: "NOT_STARTED";
  host_ssh: "DISABLED";
  shell?: "/usr/sbin/nologin";
  password_state?: "LOCKED";
  execution_enabled: boolean;
  reservation_expires_at: string;
  dry_run_at: string | null;
  allocator_result?: Record<string, unknown>;
  dry_run_result?: Record<string, unknown> | null;
  previous_plan_id?: string | null;
  failed_stage_operation_id?: string | null;
  retry_authorization_operation_id?: string | null;
};

export type SafeProvisionOperation = {
  id: string;
  operation_type: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  rollback_status: string | null;
  safe_summary: string | null;
  safe_root_cause: string | null;
  side_effect_classification: string | null;
  last_successful_step: string | null;
  first_failed_step: string | null;
  failed_handler: string | null;
  workflow_steps?: Record<string, string>;
  deployment_version?: string | null;
  canonical_execution_contract?: string | null;
  reconciliation_status?: string | null;
  resource_residue?: string[] | null;
  unknown_resource_state?: string[] | null;
};

export type ProvisionAttempt = {
  attempt_number: number;
  attempt_reason: string;
  plan: ProvisionPlan;
  operations: Array<SafeProvisionOperation | null>;
  provision_operation?: SafeProvisionOperation | null;
  stage_operation: SafeProvisionOperation | null;
  reservations: Record<string, { id: string; state: string }>;
};

export type ComputeResourceRequest = {
  id: string;
  portal_account_id: string;
  requested_by?: string;
  managed_user_id?: string | null;
  username: string;
  status: string;
  lifecycle_state?: string;
  approval_state: string;
  user_status_message?: string;
  requested_gpu_max: 0 | 1;
  requested_storage_bytes: 322122547200;
  requested_container_profile: DevelopmentContainerProfile;
  requested_lease_seconds: 345600;
  purpose: string;
  user_note: string | null;
  submitted_at: string | null;
  review_note: string | null;
  reviewed_at: string | null;
  reviewed_by?: string | null;
  approved_at: string | null;
  rejected_at: string | null;
  cancelled_at: string | null;
  created_at: string;
  updated_at: string;
  plan: ProvisionPlan | null;
  attempts?: ProvisionAttempt[];
  retry_available?: boolean;
  /** @deprecated Compatibility projection for the removed authorization ceremony. */
  retry_authorization_available?: boolean;
  retry_state?: string;
  portal_user?: {
    login_name: string;
    display_name: string;
    role: string;
    account_state: string;
    password_state: string;
    compute_state: string;
  };
};

export type FailedProvisionReconciliationResult = {
  status: "RECONCILED";
  operation_id: string;
  idempotent_replay: boolean;
  attempt_created: false;
  rollback?: "VERIFIED";
  reservation_state?: "RELEASED";
  request: ComputeResourceRequest;
};

export type FailedProvisionReconciliationReadiness = {
  status: "ZERO_VERIFIED" | "CONFLICT";
  checked_at: string;
  request_id: string;
  attempt_number: number;
  plan_id: string;
  failed_stage_operation_id: string;
  rollback_status: "REQUIRES_MANUAL_REVIEW";
  failed_hold_reservations: number;
  portal_residue: string[];
  host_residue: string[];
  unknown_resource_state: string[];
  script_integrity: "PASS" | "FAIL" | "UNKNOWN";
  state_changed: false;
  attempt_created: false;
};

export const getCsrf = () => apiFetch<{ csrf_token: string }>("/auth/csrf");
export const login = (payload: { username: string; password: string }) =>
  apiFetch<{ user: User; ssh_enrollment: SshEnrollment }>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const exchangePasswordAction = (token: string) =>
  apiFetch<{
    status: "READY";
    purpose: "INITIAL_PASSWORD_SETUP" | "PASSWORD_RESET";
    username: string;
    display_name: string;
    expires_at: string;
    one_time: true;
  }>("/auth/password-action/exchange", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
export const setupPassword = (payload: {
  password: string;
  confirmation: string;
}) =>
  apiFetch<{
    user: User;
    ssh_enrollment: SshEnrollment;
    purpose: "INITIAL_PASSWORD_SETUP" | "PASSWORD_RESET";
    requires_login: boolean;
  }>("/auth/setup-password", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export type CurrentSession = {
  user: User;
  role: string;
  ssh_enrollment: SshEnrollment;
  recent_auth_valid?: boolean;
  recent_auth_valid_until?: string | null;
};

export const me = () => apiFetch<CurrentSession>("/auth/me");
export const logout = () => apiFetch<void>("/auth/logout", { method: "POST" });
export const overview = () => apiFetch<Overview>("/platform/overview");
export const users = () =>
  apiFetch<{ status: string; users: User[]; count: number }>("/users");
export const createPortalUser = (payload: {
  login_name: string;
  display_name: string;
  role: string;
  note: string | null;
}) =>
  apiFetch<{
    status: "CREATED";
    user: User;
    operation_id: string;
    compute_resources_created: false;
  }>("/users", { method: "POST", body: JSON.stringify(payload) });
export const userDetail = (id: string) =>
  apiFetch<{ status: string; user: User }>(`/users/${encodeURIComponent(id)}`);
export const passwordActionTokens = (id: string) =>
  apiFetch<{ status: string; tokens: PasswordActionMetadata[]; count: number }>(
    `/users/${encodeURIComponent(id)}/password-action-tokens`,
  );
export const createPasswordSetupLink = (id: string) =>
  apiFetch<GeneratedPasswordActionLink>(
    `/users/${encodeURIComponent(id)}/password-setup-links`,
    { method: "POST" },
  );
export const createPasswordResetLink = (id: string) =>
  apiFetch<GeneratedPasswordActionLink>(
    `/users/${encodeURIComponent(id)}/password-reset-links`,
    { method: "POST" },
  );
export const sshKeys = (userId: string) =>
  apiFetch<SshKeyList>(`/users/${encodeURIComponent(userId)}/ssh-keys`);
export const enrollSshKey = (
  userId: string,
  payload: {
    key_type:
      "ssh-ed25519" | "ecdsa-sha2-nistp256" | "sk-ssh-ed25519@openssh.com";
    public_key: string;
    comment: string;
    scope: "HOST" | "CONTAINER" | "BOTH";
    generation_method: "BROWSER_GENERATED" | "IMPORTED";
    client_fingerprint_sha256: string;
    confirmed_private_key_saved?: boolean;
    confirmed_public_key?: boolean;
  },
) =>
  apiFetch<{
    status: string;
    key: SshKeyRecord;
    private_key_received: false;
    authorized_keys_installed: false;
  }>(`/users/${encodeURIComponent(userId)}/ssh-keys`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const activateSelfCompute = (payload: { idempotency_key: string }) =>
  apiFetch<SelfComputeActivationResult>("/self/compute/activate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const gpus = () =>
  apiFetch<{
    status: string;
    count?: number;
    gpus?: Array<Record<string, string>>;
    error?: Record<string, unknown>;
  }>("/platform/gpus");
export const gpuHealth = () =>
  apiFetch<Record<string, unknown>>("/platform/gpu-health");
export const slurmNodes = () =>
  apiFetch<Record<string, unknown>>("/slurm/nodes");
export const slurmJobs = () => apiFetch<Record<string, unknown>>("/slurm/jobs");
export const slurmHistory = () =>
  apiFetch<Record<string, unknown>>("/slurm/history");
export const slurmAccounts = () =>
  apiFetch<Record<string, unknown>>("/slurm/accounts");
export const productionPilot = () =>
  apiFetch<Record<string, unknown>>("/slurm/production-pilot");
export const containers = () =>
  apiFetch<Record<string, unknown>>("/containers");
export const containerInspect = (name: string) =>
  apiFetch<Record<string, unknown>>(`/containers/${encodeURIComponent(name)}`);
export const startManagedContainer = (
  name: string,
  payload: {
    idempotency_key: string;
    expected_compute_state: "ACTIVE";
    expected_container_state: "STOPPED";
    expected_ssh_key_state: "INSTALLED";
  },
) =>
  apiFetch<{
    status: string;
    operation_id: string;
    container: { name: string; state: string; gpu: string };
  }>(`/containers/${encodeURIComponent(name)}/start`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const storage = () =>
  apiFetch<Record<string, unknown>>("/platform/storage");
export const quotas = () =>
  apiFetch<Record<string, unknown>>("/platform/quotas");
export const registries = () =>
  apiFetch<Record<string, unknown>>("/platform/registries");
export const imageInventory = () =>
  apiFetch<Record<string, unknown>>("/images");
export const alerts = () =>
  apiFetch<Record<string, unknown>>("/platform/alerts");
export const monitoring = () =>
  apiFetch<Record<string, unknown>>("/platform/monitoring");
export const operations = () =>
  apiFetch<Record<string, unknown>>("/operations");
export const audit = () => apiFetch<Record<string, unknown>>("/audit");
export const recordPageAccess = (path: string) =>
  apiFetch<void>("/audit/page-access", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
export const sessions = () => apiFetch<PortalSession[]>("/auth/sessions");
export const revokeSession = (id: string) =>
  apiFetch<void>(`/auth/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
export const revokeOtherSessions = () =>
  apiFetch<{ revoked: number }>("/auth/sessions/revoke-others", {
    method: "POST",
  });
export const changePassword = (payload: {
  current_password: string;
  new_password: string;
  confirmation: string;
}) =>
  apiFetch<{ changed: boolean }>("/auth/password", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const reauthenticate = (password: string) =>
  apiFetch<{ reauthenticated: true }>("/auth/reauthenticate", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
export const selfComputeRequest = () =>
  apiFetch<{
    status: string;
    compute_identity: "NOT_PROVISIONED";
    request: ComputeResourceRequest | null;
  }>("/self/compute-request");
export const createSelfComputeRequest = (payload: {
  requested_gpu_max: 0 | 1;
  requested_storage_bytes: 322122547200;
  requested_container_profile: DevelopmentContainerProfile;
  requested_lease_seconds: 345600;
  purpose: string;
  user_note: string | null;
  idempotency_key: string;
}) =>
  apiFetch<{ status: "REQUESTED"; request: ComputeResourceRequest }>(
    "/self/compute-request",
    { method: "POST", body: JSON.stringify(payload) },
  );
export const cancelSelfComputeRequest = (id: string) =>
  apiFetch<{ status: "CANCELLED"; request: ComputeResourceRequest }>(
    `/self/compute-request/${encodeURIComponent(id)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: randomUuid() }),
    },
  );
export const adminComputeRequests = () =>
  apiFetch<{
    status: string;
    requests: ComputeResourceRequest[];
    count: number;
  }>("/admin/compute-resource-requests");
export const adminLeaseRecoveryIncidents = () =>
  apiFetch<{
    status: string;
    incidents: AdminLeaseRecoveryIncident[];
    count: number;
  }>("/admin/lease-recovery-incidents");
export const retryLeaseRecycle = (
  leaseId: string,
  payload: { confirmation: string; safe_reason: string },
) =>
  apiFetch<LeaseRecoveryOperationResult>(
    `/admin/compute-leases/${encodeURIComponent(leaseId)}/recycle-retry`,
    {
      method: "POST",
      body: JSON.stringify({ ...payload, idempotency_key: randomUuid() }),
    },
  );
export const adminComputeRequest = (id: string) =>
  apiFetch<{ status: string; request: ComputeResourceRequest }>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}`,
  );
export const reviewComputeRequest = (
  id: string,
  payload: {
    decision: "APPROVE" | "REJECT";
    review_note: string | null;
    idempotency_key: string;
  },
) =>
  apiFetch<{ status: string; request: ComputeResourceRequest }>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/review`,
    { method: "POST", body: JSON.stringify(payload) },
  );
export type ProvisionWorkflowResult = {
  status: "PROVISIONING" | "KEY_ENROLLMENT_PENDING" | "FAILED";
  operation_id: string;
  operation: SafeProvisionOperation;
  idempotent_replay: boolean;
  request: ComputeResourceRequest;
};

export const approveAndProvision = (
  id: string,
  payload: { review_note: string | null; idempotency_key: string },
) =>
  apiFetch<ProvisionWorkflowResult>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/approve-and-provision`,
    { method: "POST", body: JSON.stringify(payload) },
  );

export const retryProvision = (
  id: string,
  payload: { admin_note: string | null; idempotency_key: string },
) =>
  apiFetch<ProvisionWorkflowResult>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/retry`,
    { method: "POST", body: JSON.stringify(payload) },
  );

/** @deprecated Compatibility-only endpoint; the Portal no longer calls it. */
export const authorizeProvisionRetry = (
  id: string,
  payload: {
    failure_classification: "NO_SIDE_EFFECT" | "PARTIAL_ROLLED_BACK";
    safe_root_cause: string;
    authorization_reason: string;
    remediation_git_commit: string;
  },
) =>
  apiFetch<{
    status: "RETRY_AUTHORIZED";
    operation_id: string;
    idempotent_replay: boolean;
    request: ComputeResourceRequest;
  }>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/retry-authorize`,
    {
      method: "POST",
      body: JSON.stringify({ ...payload, idempotency_key: randomUuid() }),
    },
  );
export const failedProvisionReconciliationReadiness = (
  id: string,
  planId: string,
  failedStageOperationId: string,
) => {
  const query = new URLSearchParams({
    plan_id: planId,
    failed_stage_operation_id: failedStageOperationId,
  });
  return apiFetch<FailedProvisionReconciliationReadiness>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/failed-provision-reconciliation-readiness?${query.toString()}`,
  );
};
export const reconcileFailedProvision = (
  id: string,
  payload: {
    plan_id: string;
    failed_stage_operation_id: string;
    review_note: string;
  },
) =>
  apiFetch<FailedProvisionReconciliationResult>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/reconcile-failed-provision`,
    {
      method: "POST",
      body: JSON.stringify({ ...payload, idempotency_key: randomUuid() }),
    },
  );
/** @deprecated Compatibility-only endpoint; orchestration creates plans internally. */
export const createProvisionPlan = (id: string) =>
  apiFetch<{ status: string; plan: ProvisionPlan }>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/plan`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: randomUuid() }),
    },
  );
/** @deprecated Compatibility-only endpoint; orchestration runs dry-run internally. */
export const dryRunProvisionPlan = (id: string) =>
  apiFetch<{ status: "READY_FOR_PROVISION"; plan: ProvisionPlan }>(
    `/admin/compute-resource-requests/${encodeURIComponent(id)}/dry-run`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: randomUuid() }),
    },
  );
/** @deprecated Compatibility-only endpoint; orchestration runs Stage internally. */
export const provisionComputeEnvironment = (id: string) =>
  apiFetch<{
    status: "KEY_ENROLLMENT_PENDING";
    operation_id: string;
    managed_user_id: string;
    plan: ProvisionPlan;
    idempotent_replay: boolean;
  }>(`/admin/compute-resource-requests/${encodeURIComponent(id)}/provision`, {
    method: "POST",
    body: JSON.stringify({ idempotency_key: randomUuid() }),
  });
export const selfEnvironment = () =>
  apiFetch<{ status: string; environment: SelfEnvironment }>(
    "/self/environment",
  );
export const selfLease = () =>
  apiFetch<{ status: string; lease: ComputeLease }>("/self/lease");
export const requestLeaseRenewal = (payload: {
  duration_seconds: number;
  idempotency_key: string;
}) =>
  apiFetch<{ status: string; renewal_request_id: string }>(
    "/self/lease/renewals",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
export const selfContainer = () =>
  apiFetch<{ status: string; container: SelfEnvironment["container"] }>(
    "/self/container",
  );
export const selfContainerConnection = () =>
  apiFetch<{
    status: string;
    connection: {
      available: boolean;
      host: string;
      port: number;
      username: string;
      authentication: string;
      profile: SelfEnvironment["container"]["profile"];
      gpu: "NONE" | "SLURM_ALLOCATED_1";
      key_fingerprint: string | null;
      command: string | null;
      vscode: string | null;
    };
  }>("/self/container/connection");
export const selfContainerAction = (action: "start" | "stop" | "restart") =>
  apiFetch<{ status: string; operation_id: string; state: string }>(
    `/self/container/${action}`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: randomUuid() }),
    },
  );
export const startSelfTerminal = (payload: {
  idempotency_key: string;
  cols: number;
  rows: number;
}) =>
  apiFetch<{ status: string; terminal: SelfTerminal }>(
    "/self/container/terminal/sessions",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
export const selfTerminalOutput = (
  id: string,
  cursor: number,
  signal?: AbortSignal,
) =>
  apiFetch<{
    status: string;
    terminal_id: string;
    data_b64: string;
    cursor: number;
    state: string;
    reason: string | null;
    exit_code: number | null;
  }>(
    `/self/container/terminal/sessions/${encodeURIComponent(id)}/output?cursor=${cursor}`,
    { signal },
  );
export const sendSelfTerminalInput = (id: string, data: string) =>
  apiFetch<{ status: string }>(
    `/self/container/terminal/sessions/${encodeURIComponent(id)}/input`,
    {
      method: "POST",
      body: JSON.stringify({ data }),
    },
  );
export const resizeSelfTerminal = (id: string, cols: number, rows: number) =>
  apiFetch<{ status: string }>(
    `/self/container/terminal/sessions/${encodeURIComponent(id)}/resize`,
    {
      method: "POST",
      body: JSON.stringify({ cols, rows }),
    },
  );
export const closeSelfTerminal = (id: string) =>
  apiFetch<{ status: string }>(
    `/self/container/terminal/sessions/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
export const selfJobs = () =>
  apiFetch<{ status: string; jobs: SelfJob[]; count: number }>("/self/jobs");
export const submitSelfJob = (payload: {
  name: string;
  script: string;
  cpus: number;
  memory_mb: number;
  gpu_count: 0 | 1;
  time_limit_seconds: number;
  image_ref: string | null;
  idempotency_key: string;
}) =>
  apiFetch<{ status: string; job: SelfJob }>("/self/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const selfJobLogs = (id: string) =>
  apiFetch<{ status: string; stdout: string; stderr: string }>(
    `/self/jobs/${encodeURIComponent(id)}/logs`,
  );
export const cancelSelfJob = (id: string) =>
  apiFetch<{ status: string; job: SelfJob }>(
    `/self/jobs/${encodeURIComponent(id)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: randomUuid() }),
    },
  );
export const selfStorage = () =>
  apiFetch<{
    status: string;
    storage: {
      root: string;
      quota_bytes: number | null;
      used_bytes?: number | null;
      available_bytes?: number | null;
      state: string;
      private: true;
    };
  }>("/self/storage");
export const selfRecycleBin = () =>
  apiFetch<{
    status: string;
    items: Array<{
      id: string;
      resource_name: string;
      state: string;
      expires_at: string;
      recycled_at: string;
      data_preserved: boolean;
      auto_permanent_delete: false;
      container: "STOPPED";
    }>;
    auto_permanent_delete: false;
  }>("/self/recycle-bin");
export const requestRestore = (itemId: string, durationSeconds = 345600) =>
  apiFetch<{
    status: string;
    restore_request_id: string;
    lease_id: string | null;
  }>(`/self/recycle-bin/${encodeURIComponent(itemId)}/restore-requests`, {
    method: "POST",
    body: JSON.stringify({
      duration_seconds: durationSeconds,
      idempotency_key: randomUuid(),
    }),
  });
export type AdminRestoreRequest = {
  id: string;
  username: string;
  resource_name: string;
  state: string;
  duration_seconds: number;
  requested_at: string;
  decided_at: string | null;
};

export const adminRestoreRequests = () =>
  apiFetch<{ status: "OK"; requests: AdminRestoreRequest[]; count: number }>(
    "/admin/restore-requests",
  );

export const decideRestoreRequest = (
  requestId: string,
  decision: "APPROVE" | "REJECT",
  comment: string | null,
) =>
  apiFetch<{ status: string; restore_request_id?: string; lease_id?: string }>(
    `/admin/restore-requests/${encodeURIComponent(requestId)}/decision`,
    {
      method: "POST",
      body: JSON.stringify({ decision, comment }),
    },
  );
export const createOperation = (payload: Record<string, unknown>) =>
  apiFetch<Record<string, unknown>>("/operations", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const submitOperation = (id: string, confirmation: string) =>
  apiFetch<Record<string, unknown>>(
    `/operations/${encodeURIComponent(id)}/submit`,
    {
      method: "POST",
      body: JSON.stringify({ confirmation }),
    },
  );
export const approveOperation = (
  id: string,
  payload: {
    decision: "APPROVE" | "REJECT";
    confirmation: string;
    comment?: string;
  },
) =>
  apiFetch<Record<string, unknown>>(
    `/operations/${encodeURIComponent(id)}/approval`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
