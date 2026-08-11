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
        description="用户申请与服务器资源创建严格分离"
        action={<StatusBadge value={`${query.data.count} REQUESTS`} />}
      />
      <Card style={{ padding: 14 }}>
        <ObjectTable rows={rows} columns={COLUMNS} />
      </Card>
      <div className="section-grid">
        <SectionCard title="审批边界">
          批准只改变申请状态；UID、Project ID 和端口由 allocator 生成并在 Portal
          DB reservation。
        </SectionCard>
        <SectionCard title="执行边界">
          Dry-run 只读检查宿主。正式创建计算环境在本阶段保持 disabled，Lease
          也不会开始。
        </SectionCard>
      </div>
    </>
  );
}
