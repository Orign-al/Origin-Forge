import { ApiError, apiFetch } from "@h100-portal/api-client";

export { ApiError };

export type User = {
  id: string;
  login_name: string;
  normalized_login: string;
  display_name: string;
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
    gpu: "NONE";
    cpus: number;
    memory_gb: number;
    pids_limit: number;
  };
  storage: { quota_bytes: number | null; state: string };
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
  gpu: "NONE";
  host_access: "DISABLED";
  state: string;
  expires_at: string;
  idle_timeout_seconds: number;
  max_duration_seconds: number;
};

export const getCsrf = () => apiFetch<{ csrf_token: string }>("/auth/csrf");
export const login = (payload: { username: string; password: string }) =>
  apiFetch<{ user: User; ssh_enrollment: SshEnrollment }>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const setupPassword = (payload: {
  token: string;
  password: string;
  confirmation: string;
}) =>
  apiFetch<{ user: User; ssh_enrollment: SshEnrollment }>(
    "/auth/setup-password",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
export const me = () =>
  apiFetch<{ user: User; role: string; ssh_enrollment: SshEnrollment }>(
    "/auth/me",
  );
export const logout = () => apiFetch<void>("/auth/logout", { method: "POST" });
export const overview = () => apiFetch<Overview>("/platform/overview");
export const users = () =>
  apiFetch<{ status: string; users: User[]; count: number }>("/users");
export const userDetail = (id: string) =>
  apiFetch<{ status: string; user: User }>(`/users/${encodeURIComponent(id)}`);
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
      gpu: "NONE";
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
      body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
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
  script_path: string;
  workdir: string;
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
      body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
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
  apiFetch<{ status: string; restore_request_id: string }>(
    `/self/recycle-bin/${encodeURIComponent(itemId)}/restore-requests`,
    {
      method: "POST",
      body: JSON.stringify({
        duration_seconds: durationSeconds,
        idempotency_key: crypto.randomUUID(),
      }),
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
