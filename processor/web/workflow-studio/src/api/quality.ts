/** 数据质检 API —— 检查项目录与报告。
 *
 * 检查项目录是设置面板的**唯一数据源**：面板不硬编码任何检查项，
 * 全靠它渲染 tab、开关和阈值输入框。后端新增检查项前端零改动。
 */
import { req } from './workflows';

const BASE = '/api/v1/quality';

/** 单个检查项的目录条目 —— 对应后端 all_check_specs() 的一项。 */
export interface CheckSpec {
  slug: string;
  category: string;
  label: string;
  version: string;
  description: string;
  requires_channels: string[];
  requires_modality: string[];
  cross_modal: boolean;
  /** 属于哪些设备卡片 —— 设置面板按它分 tab */
  device_cards: string[];
  default_severity: string;
  /** 阈值默认值；面板按它动态生成输入框 */
  default_params: Record<string, number | string | boolean>;
}

export interface CheckCatalog {
  ruleset_revision: string;
  checks: CheckSpec[];
  /** 设备卡片 → 该卡片下的检查项 slug 列表 */
  by_device: Record<string, string[]>;
  device_labels: Record<string, string>;
  device_channels: Record<string, string[]>;
  severity_labels: Record<string, string>;
  total: number;
}

/** 一个检查项的节点配置 —— 存进 node.data.config.devices[卡片].checks[slug] */
export interface CheckConfig {
  enabled?: boolean;
  params?: Record<string, number>;
}

/** 一张设备卡片的配置 */
export interface DeviceConfig {
  checks?: Record<string, CheckConfig>;
}

/** data_quality 节点的完整配置结构 */
export interface QualityNodeConfig {
  devices?: Record<string, DeviceConfig>;
}

export function getCheckCatalog(): Promise<CheckCatalog> {
  return req<CheckCatalog>(`${BASE}/checks`);
}

export function listQualityReports(project?: string, status?: string) {
  const p = new URLSearchParams();
  if (project) p.set('project', project);
  if (status) p.set('status', status);
  return req<{ reports: unknown[]; total: number; counts: Record<string, number> }>(
    `${BASE}/summary?${p.toString()}`,
  );
}

export function getQualityReport(episodeId: string) {
  return req<Record<string, unknown>>(
    `${BASE}/episodes/${encodeURIComponent(episodeId)}`,
  );
}

export function runQualityChecks(episodeId: string, probeVideo = false) {
  return req<Record<string, unknown>>(
    `${BASE}/episodes/${encodeURIComponent(episodeId)}/run?probe_video=${probeVideo}`,
    { method: 'POST' },
  );
}
