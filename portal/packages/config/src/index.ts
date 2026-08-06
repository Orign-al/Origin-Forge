export const PORTAL_TITLE = "H100 管理平台";
export const ACCESS_MODE_LABEL = "INTERNAL HTTP · VIRTUAL NETWORK ONLY";
export const ACCESS_MODE_STATUS =
  "INTERNAL HTTP ACCEPTED BY ADMINISTRATOR — VIRTUAL NETWORK ONLY";
export const TRANSPORT_TLS_STATUS =
  "NOT ENABLED — NOT REQUIRED FOR CURRENT PILOT SCOPE";
export const API_PREFIX = "/api/v1";

export const navigation = [
  ["总览", "/"],
  ["用户", "/users"],
  ["容器", "/containers"],
  ["Slurm", "/slurm"],
  ["作业", "/jobs"],
  ["GPU", "/gpus"],
  ["存储", "/storage"],
  ["镜像", "/images"],
  ["监控", "/monitoring"],
  ["审批", "/operations"],
  ["审计", "/audit"],
  ["系统", "/system"],
] as const;
