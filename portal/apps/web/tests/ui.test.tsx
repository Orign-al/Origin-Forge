import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EmptyState, StatusBadge } from "@h100-portal/ui";
import {
  ObjectTable,
  type SimpleColumnDef,
  type SimpleRow,
} from "../components/Tables";

describe("portal UI primitives", () => {
  it("renders status text as well as a status color", () => {
    render(<StatusBadge value="DRAIN" />);
    const badge = screen.getByText("DRAIN");
    expect(badge).toHaveClass("ui-badge-warning");
  });

  it("renders explicit empty-state detail", () => {
    render(<EmptyState title="暂无数据" detail="数据源没有返回记录" />);
    expect(screen.getByText("暂无数据")).toBeInTheDocument();
    expect(screen.getByText("数据源没有返回记录")).toBeInTheDocument();
  });

  it("renders a compact TanStack table", () => {
    const columns: SimpleColumnDef[] = [
      { accessorKey: "name", header: "名称" },
      { accessorKey: "state", header: "状态" },
    ];
    const rows: SimpleRow[] = [
      { id: "one", name: "sagsh100server", state: "DRAIN" },
    ];
    render(<ObjectTable rows={rows} columns={columns} />);
    expect(
      screen.getByRole("columnheader", { name: "名称" }),
    ).toBeInTheDocument();
    expect(screen.getByText("sagsh100server")).toBeInTheDocument();
  });
});
