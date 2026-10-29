// Operational API only. Business types are generated from the existing contract.
export interface ProviderStatus {
  id: string;
  name: string;
  owner: string;
  capabilities: string[];
  state: string;
  note: string;
}
export interface SystemStatus {
  version: string;
  stage: "research_engine";
  discovery_mode: string;
  autonomous_investigation: boolean;
  bounded_investigation: string;
  environment: string;
  intelligence_ready: boolean;
  dependencies: Record<string, string>;
  providers: ProviderStatus[];
  research_stages: { key: string; label: string }[];
}

export async function getSystemStatus(
  signal?: AbortSignal,
): Promise<SystemStatus> {
  const response = await fetch("/api/system/status", {
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error("暂时无法连接服务，请重试。");
  return (await response.json()) as SystemStatus;
}
