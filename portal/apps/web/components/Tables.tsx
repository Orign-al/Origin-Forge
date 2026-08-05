"use client";

import { tableFeatures, useTable, type ColumnDef } from "@tanstack/react-table";

import { DataTable, EmptyState, StatusBadge } from "@h100-portal/ui";

export type SimpleRow = Record<string, unknown> & { id?: string };
const SIMPLE_TABLE_FEATURES = tableFeatures({});
export type SimpleColumnDef = ColumnDef<
  typeof SIMPLE_TABLE_FEATURES,
  SimpleRow
>;

export function ObjectTable({
  rows,
  columns,
  empty = "暂无数据",
}: {
  rows: SimpleRow[];
  columns: SimpleColumnDef[];
  empty?: string;
}) {
  const table = useTable({
    data: rows,
    columns,
    features: SIMPLE_TABLE_FEATURES,
  });
  if (rows.length === 0)
    return <EmptyState title={empty} detail="数据源没有返回记录" />;
  return (
    <DataTable>
      <thead>
        {table.getHeaderGroups().map((group) => (
          <tr key={group.id}>
            {group.headers.map((header) => (
              <th key={header.id}>
                {header.isPlaceholder ? null : (
                  <table.FlexRender header={header} />
                )}
              </th>
            ))}
          </tr>
        ))}
      </thead>
      <tbody>
        {table.getRowModel().rows.map((row) => (
          <tr key={row.id}>
            {row.getAllCells().map((cell) => (
              <td key={cell.id}>
                <table.FlexRender cell={cell} />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

export function ValueCell({ value }: { value: unknown }) {
  if (
    typeof value === "string" &&
    [
      "OK",
      "UNKNOWN",
      "PARTIAL",
      "ACTIVE",
      "INVITED",
      "DRAIN",
      "IDLE+DRAIN",
      "DEFERRED",
      "RUNNING",
      "PENDING",
      "FAILED",
      "SUCCEEDED",
      "NOT_ENROLLED",
      "NOT_APPLIED",
    ].includes(value)
  )
    return <StatusBadge value={value} />;
  if (value === null || value === undefined || value === "")
    return <span className="muted">—</span>;
  if (typeof value === "object")
    return <span className="mono">{JSON.stringify(value)}</span>;
  return <span>{String(value)}</span>;
}
