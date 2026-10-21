CREATE INDEX cases_updated_idx ON cases(updated_at DESC);
CREATE INDEX platform_lists_position_idx ON platform_list_entries(scope,position,case_id);
CREATE INDEX visits_case_stage_idx ON visits(case_id,stage);
CREATE INDEX platform_reviews_case_stage_idx ON platform_review_batches(case_id,stage,created_at DESC);
