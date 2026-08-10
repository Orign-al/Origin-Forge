"use client";

import { useQuery } from "@tanstack/react-query";

import {
  ErrorBlock,
  LoadingBlock,
  UnauthorizedBlock,
} from "../../../components/PortalShell";
import { WebTerminal } from "../../../components/WebTerminal";
import { me } from "../../../lib/api";

export default function TerminalPage() {
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });
  if (current.isPending) return <LoadingBlock />;
  if (current.isError) return <ErrorBlock />;
  return current.data.role === "user" ? <WebTerminal /> : <UnauthorizedBlock />;
}
