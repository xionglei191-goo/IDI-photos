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
export interface PlatformAccountStatus {
  project_id?: string; enabled?: boolean; write_enabled?: boolean; automatic_paused?: boolean;
  configured: boolean; username: string; password_stored: boolean; auto_connect: boolean; state: string;
  last_attempt_at: string | null; last_success_at: string | null; next_retry_at: string | null; error: string;
  session: PlatformSession;
}
export interface PlatformLink {
  case_id: string; report_receiving_id: string; platform_report_code: string; insurer_report_number: string;
  verified_at: string | null; revision: number; node: string; snapshot: { scope?: string; account?: string };
}
export interface PlatformDraft { revision: number; values: Record<string, string>; files: Record<string, string[]>; updated_at: string | null; target_report_id: string }
export interface OfficialFile {
  id: string; fileUrl: string; fileName?: string; oldFileName?: string; name?: string; fileType?: string;
}
export interface AttachmentRemoval {
  id: string; case_id: string; stage: PlatformStage; field_key: string; file_id: string;
  fingerprint: string; state: 'prepared' | 'removing' | 'saved' | 'uncertain' | 'verified' | 'failed' | 'stale';
  error: string; file: OfficialFile; label: string; stage_label: string; case_title: string; short_code: string;
}
export interface PlatformFileField extends PlatformField {
  selected: Photo[]; existing: OfficialFile[];
  removed?: (OfficialFile & { removed_at: string })[];
  suggest?: [string, string];
  suggested_ids: string[]; count: number;
}
export interface PlatformFieldSuggestion { value: string; source: string; photo_ids: string[]; needs_review: boolean }
export interface PlatformFieldSource extends PlatformFieldSuggestion { origin: 'automatic' | 'manual'; current: boolean; stale: boolean }
export interface PlatformPlan {
  review_only?: boolean; pending_fields?: { field: string; label: string; message: string }[];
  write_binding?: { project_id: string; username: string; revision: string; enabled: boolean };
  case: Case; stage: PlatformStage; label: string; guide_page: number; contract_version: string;
  link: PlatformLink; draft: PlatformDraft; values: Record<string, string | number>;
  remote_values: Record<string, string>;
  fields: PlatformField[]; file_fields: PlatformFileField[]; photos: Photo[];
  photo_total: number; unassigned_count: number; error_count: number; warning_count: number;
  issues: { field: string; message: string; severity: 'error' | 'warning'; code?: string }[];
  personnel: { userId: string; name: string; phone: string }[];
  business_fields: Record<string, unknown>; endpoint: string; actions: string[];
  write_enabled: boolean; remote_written: false; prepared_for_review: boolean;
  access: StageAccess; view_source: 'official' | 'local';
  field_suggestions?: Record<string, PlatformFieldSuggestion>; field_suggestion_notes?: Record<string, string>;
  field_sources?: Record<string, PlatformFieldSource>;
  attachment_removals?: AttachmentRemoval[];
}
export interface PlatformPackage { id: string; label: string; stage: string; created_at: string; file_count: number; error_count: number; download_url: string }
export interface RemoteCase { reportReceivingId: string; reportCode: string; reportNumber: string | null; communityName: string; communityDesc: string; accidentDesc: string; issueDescription?: string }
