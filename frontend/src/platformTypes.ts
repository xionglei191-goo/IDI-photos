import type { Case, Photo } from './types';

export type PlatformStage = 'intake' | 'survey' | 'repair' | 'acceptance';
export interface StageAccess {
  editable: boolean; mode: 'editable' | 'completed' | 'waiting' | 'future' | 'closed' | 'unverified';
  reason: string; active_stage: PlatformStage | null; active_label: string; workflow_stage: string;
}
export interface PlatformOption { code: string; name: string }
export interface PlatformField {
  key: string; name_key?: string; label: string; kind: string; required: boolean; readonly?: boolean;
  options?: PlatformOption[]; dictionary?: string; when?: Record<string, string[]>;
  extensions?: string[]; hint?: string; checkin?: boolean; max_bytes_exclusive?: number;
}
export interface PlatformContract {
  version: string; verified_on: string; dictionary_verified_at: string; website: string;
  dictionaries: Record<string, PlatformOption[]>;
  stages: Record<PlatformStage, { label: string; guide_page: number; fields: PlatformField[] }>;
}
export interface PlatformSession { connected: boolean; account: string; mode: string; connected_at: string | null; write_enabled: boolean }
export interface PlatformLink {
  case_id: string; report_receiving_id: string; platform_report_code: string; insurer_report_number: string;
  verified_at: string | null; revision: number; node: string; snapshot: { scope?: string; account?: string };
}
export interface PlatformDraft { revision: number; values: Record<string, string>; files: Record<string, string[]>; updated_at: string | null; target_report_id: string }
export interface PlatformFileField extends PlatformField {
  selected: Photo[]; existing: { id: string; fileUrl: string; fileName?: string; oldFileName?: string; name?: string }[];
  suggested_ids: string[]; count: number;
}
export interface PlatformFieldSuggestion { value: string; source: string; photo_ids: string[]; needs_review: boolean }
export interface PlatformPlan {
  case: Case; stage: PlatformStage; label: string; guide_page: number; contract_version: string;
  link: PlatformLink; draft: PlatformDraft; values: Record<string, string | number>;
  remote_values: Record<string, string>;
  fields: PlatformField[]; file_fields: PlatformFileField[]; photos: Photo[];
  photo_total: number; unassigned_count: number; error_count: number; warning_count: number;
  issues: { field: string; message: string; severity: 'error' | 'warning' }[];
  personnel: { userId: string; name: string; phone: string }[];
  business_fields: Record<string, unknown>; endpoint: string; actions: string[];
  write_enabled: boolean; remote_written: false; prepared_for_review: boolean;
  access: StageAccess; view_source: 'official' | 'local';
  field_suggestions?: Record<string, PlatformFieldSuggestion>; field_suggestion_notes?: Record<string, string>;
}
export interface PlatformPackage { id: string; label: string; stage: string; created_at: string; file_count: number; error_count: number; download_url: string }
export interface RemoteCase { reportReceivingId: string; reportCode: string; reportNumber: string | null; communityName: string; communityDesc: string; accidentDesc: string; issueDescription?: string }
