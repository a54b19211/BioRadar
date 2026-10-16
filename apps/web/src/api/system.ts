// Operational API only. Business types are generated from the existing contract.
export interface ProviderStatus {
  id: string;
  name: string;
  owner: string;
  capabilities: string[];
  state: "not_connected";
  note: string;
}
export interface SystemStatus {
  version: string;
  stage: "research_engine";
  discovery_mode: "frozen_snapshot";
  autonomous_investigation: false;
  bounded_investigation: "data_fabric_cli";
  environment: string;
  intelligence_ready: false;
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
