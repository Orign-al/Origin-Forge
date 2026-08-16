"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Card, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../components/PortalShell";
import { ObjectTable, type SimpleColumnDef } from "../../../components/Tables";
import { adminComputeRequests } from "../../../lib/api";

function localTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

const COLUMNS: SimpleColumnDef[] = [
  {
    accessorKey: "username",
    header: "用户",
    cell: (info) => (
      <Link
        className="table-link"
        href={`/compute-requests/${String(info.row.original.id)}`}
      >
        {String(info.getValue())}
      </Link>
    ),
  },
  {
    accessorKey: "status",
    header: "状态",
    cell: (info) => <StatusBadge value={String(info.getValue())} />,
  },
  { accessorKey: "gpu", header: "GPU" },
  { accessorKey: "storage", header: "Storage" },
  { accessorKey: "container", header: "Container Profile" },
  { accessorKey: "lease", header: "Lease" },
  { accessorKey: "purpose", header: "用途" },
  { accessorKey: "submitted", header: "提交时间" },
];

export default function ComputeRequestsPage() {
  const query = useQuery({
    queryKey: ["admin-compute-requests"],
    queryFn: adminComputeRequests,
    refetchInterval: 20_000,
  });
  if (query.isPending) {
    return (
      <>
        <PageHeading title="计算资源申请" />
        <LoadingBlock />
      </>
    );
  }
  if (query.isError) {
    return (
      <>
        <PageHeading title="计算资源申请" />
        <ErrorBlock message="当前账号无权查看申请，或申请数据暂时不可用。" />
      </>
    );
  }
  const rows = query.data.requests.map((item) => ({
    ...item,
    status: item.lifecycle_state ?? item.status,
    gpu: item.requested_gpu_max,
    storage: "300GB",
    container: "8CPU / 32GB / GPU NONE",
    lease: "96h after activation",
    submitted: localTime(item.submitted_at),
  }));
  return (
    <>
      <PageHeading
        title="计算资源申请"
        description="一次批准或一次 Retry；安全检查由后端自动编排"
        action={<StatusBadge value={`${query.data.count} REQUESTS`} />}
      />
      <Card style={{ padding: 14 }}>
        <ObjectTable rows={rows} columns={COLUMNS} />
      </Card>
      <div className="section-grid">
        <SectionCard title="审批边界">
          管理员批准一次后，allocator、Reservation、Dry-run 与 Stage
          由后端自动完成。
        </SectionCard>
        <SectionCard title="执行边界">
          Stage 前继续执行固定 Worker safety gate；Lease 不会提前开始。
        </SectionCard>
      </div>
    </>
  );
}
