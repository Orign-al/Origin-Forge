"use client";

import { useQuery } from "@tanstack/react-query";

import { ContainersModule } from "../../../components/ModulePages";
import { OrdinaryContainer } from "../../../components/OrdinaryUserPages";
import { ErrorBlock, LoadingBlock } from "../../../components/PortalShell";
import { me } from "../../../lib/api";

export default function ContainersPage() {
  const current = useQuery({ queryKey: ["me"], queryFn: me });
  if (current.isPending) return <LoadingBlock />;
  if (current.isError) return <ErrorBlock />;
  return current.data.role === "user" ? (
    <OrdinaryContainer />
  ) : (
    <ContainersModule />
  );
}
