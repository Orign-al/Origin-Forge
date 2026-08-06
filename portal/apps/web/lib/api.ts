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

export const getCsrf = () => apiFetch<{ csrf_token: string }>("/auth/csrf");
export const login = (payload: { username: string; password: string }) =>
  apiFetch<{ user: User }>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const setupPassword = (payload: {
  token: string;
  password: string;
  confirmation: string;
}) =>
  apiFetch<{ user: User }>("/auth/setup-password", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const me = () => apiFetch<{ user: User; role: string }>("/auth/me");
export const logout = () => apiFetch<void>("/auth/logout", { method: "POST" });
export const overview = () => apiFetch<Overview>("/platform/overview");
export const users = () =>
  apiFetch<{ status: string; users: User[]; count: number }>("/users");
export const userDetail = (id: string) =>
  apiFetch<{ status: string; user: User }>(`/users/${encodeURIComponent(id)}`);
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
export const containers = () =>
  apiFetch<Record<string, unknown>>("/containers");
export const containerInspect = (name: string) =>
  apiFetch<Record<string, unknown>>(`/containers/${encodeURIComponent(name)}`);
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
