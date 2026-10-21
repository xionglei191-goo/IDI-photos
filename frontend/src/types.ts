export interface Project { id: string; name: string; aliases: string[]; insurer: string; short_prefix: string; case_count: number; photo_count: number; updated_case_codes?: number }
export interface Case {
  id: string; project_id: string; project_name: string; report_number: string | null; title: string;
  building: string; unit: string; floor: string; room: string; location_type: string; location_detail: string;
  insurer: string; contact_name: string; contact_phone: string; repair_parts: string[]; description: string;
  report_at: string | null; submitted_at: string | null; quoted_at: string | null; accepted_at: string | null;
  status: string; revision: number; photo_count: number; created_at: string; updated_at: string;
  short_code: string | null; short_code_aliases: string[]; platform_report_code: string | null; policy_number: string;
  workflow_stage: string; workflow_source: string; workflow_verified_at: string | null;
  responsible: string; insurer_template_id: string; remote_updated_at: string | null;
  platform_lists: ('intake' | 'repair')[]; platform_linked: boolean; followup_eligible: boolean;
}
export interface Source { id: string; source_type: string; original_name: string; original_path: string; group_name: string; sender: string; message_time: string | null; received_at: string; metadata: Record<string, unknown> }
export interface Candidate { case_id: string; case_title: string; report_number: string | null; project_name: string; location: string; score: number; reasons: string[] }
export interface Recognition { id: string; provider: string; state: string; raw_text: string; suggestions: { fields?: Record<string, string>; evidence?: unknown }; candidates: Candidate[]; error: string | null; created_at: string }
export interface Photo {
  uploaded_by?: string | null; uploaded_by_name?: string | null;
  processing?: { receipt: string; pipeline: { state?: string; identity?: string; reason?: string }; draft: Record<string, unknown>; rule: { state: string; error: string | null } | null; ai: { id: string; state: string; attempts: number; error: string | null; result?: Record<string, unknown> } | null };
  id: string; blob_hash: string; project_id: string | null; case_id: string | null; visit_id: string | null;
  visit_date?: string | null; visit_key?: string | null; shared_checkin_visit_id?: string | null; shared_checkin_date?: string | null; shared_checkin_key?: string | null;
  reuse_source_id?: string | null; reuse_source_exists?: boolean; reuse_source_revision?: number | null; reuse_copy_id?: string | null;
  case_title: string | null; project_name: string | null; report_number: string | null; building: string | null; room: string | null; unit: string | null; insurer: string | null;
  stage: string; role: string; photo_group: string; short_code: string | null; platform_report_code: string | null; part: string; process: string; watermark_time: string | null; exif_time: string | null;
  notes: string; recognition_state: string; confirmed_fields: string[]; revision: number; media_type: string;
  watermark_code?: string; watermark_code_source?: string; watermark_dedup_reason?: string;
  size_bytes: number; width: number | null; height: number | null; original_name: string; thumbnail_url: string | null;
  original_url: string; sender: string; group_name: string; message_time: string | null; source_type: string; source_count: number;
  created_at: string; updated_at: string; sources?: Source[]; recognitions?: Recognition[];
}
export interface PhotoPage { items: Photo[]; total: number; page: number; page_size: number }
export interface Summary { total: number; unassigned: number; unknown: number; failed: number; exceptions: number; cases: number; active_jobs: number; attachment_failures: number; stages: Record<string, number> }
export interface Rule { stage: string; role: string; photo_group?: string; minimum: number; per_visit: boolean; part?: string; process?: string }
export interface Template { id: string; name: string; insurer: string; requirements: Rule[] }
export interface Check { stage: string; role: string; photo_group?: string; part: string; process?: string; scope: string; minimum: number; count: number; missing: number; photo_ids: string[] }
export interface Completeness { case_id: string; case_title: string; total_images: number; missing_count: number; items: Check[] }
export interface ExportPreview { photo_count: number; cases: { case: Case; photo_ids: string[]; completeness: Completeness }[] }
export interface Job { id: string; kind: string; state: string; progress: number; total: number; attempts: number; payload: Record<string, unknown>; result: { batch_id?: string; packages?: { id: string; download_url: string }[]; download_url?: string; errors?: { photo_id?: string; error: string }[]; [key: string]: unknown } | null; error: string | null; created_at: string; finished_at: string | null }
export interface Package { id: string; case_id: string; case_title: string; project_name: string; parent_id: string | null; version: number; title: string; state: string; photo_count: number; missing_count: number; created_at: string; download_url: string; zip_hash: string }
export interface PackageDetail extends Package { manifest: { case: Case; photos: Photo[]; completeness: Completeness; version: number; created_at: string }; events: { id: string; state: string; event_at: string; note: string; attachment_hash: string | null; created_at: string }[] }
export interface Connector { id: string; name: string; kind: string; enabled: boolean; status: string; error: string | null; last_seen_at: string | null; wxauto_available: boolean; secret_configured?: boolean; config: { project_id: string | null; case_id: string | null; path: string; groups: string[]; auto_ocr: boolean; app_id?: string; chat_id?: string; chat_name?: string }; counts: { messages: number; saved: number; failed: number; pending: number }; capture?: { phase: string; last_event_at: string | null; last_history_at: string | null }; queue?: { total: number; completed: number; failed: number; pending: number; last_error?: string } }
export interface RecognitionConfig { local_ocr: boolean; local_ocr_available: boolean; vision_enabled: boolean; base_url: string; model: string; key_env: string; vision_key_present: boolean; vision_key_stored?: boolean; vision_ready: boolean }
export interface System { deployment?: "local" | "vps"; version: string; data_dir: string; local_only: boolean; lan_urls: string[]; public_urls: string[]; stages: Record<string, string>; roles: Record<string, string>; case_statuses: Record<string, string>; recognition: RecognitionConfig; max_file_mb: number }
export interface Filter { project_id: string; case_id: string; stage: string; review: string; photo_group?: string }
export interface Batch { id: string; kind: string; state: string; total: number; imported: number; duplicates: number; failed: number; created_at: string; finished_at: string | null; details: Record<string, unknown>; items?: { id: string; source_name: string; status: string; photo_id: string | null; error: string | null }[] }
export type Notify = (message: string, error?: boolean) => void;
