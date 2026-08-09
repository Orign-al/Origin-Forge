"use client";

import { useQuery } from "@tanstack/react-query";

import { JobsModule } from "../../../components/ModulePages";
import { OrdinaryJobs } from "../../../components/OrdinaryUserPages";
import { ErrorBlock, LoadingBlock } from "../../../components/PortalShell";
import { me } from "../../../lib/api";

export default function JobsPage() {
  const current = useQuery({ queryKey: ["me"], queryFn: me });
  if (current.isPending) return <LoadingBlock />;
  if (current.isError) return <ErrorBlock />;
  return current.data.role === "user" ? <OrdinaryJobs /> : <JobsModule />;
}
