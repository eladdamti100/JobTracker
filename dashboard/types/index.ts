export type JobStatus =
  | "new"
  | "scored"
  | "notified"
  | "approved"
  | "applying"
  | "applied"
  | "failed"
  | "in_review"
  | "rejected"
  | "interview"
  | "next_stage"
  | "accepted";

export type JobLevel = "student" | "junior" | "senior";
export type JobSource = "HireMeTech" | "LinkedIn" | "WhatsApp" | "Unknown";
export type ReferralType = "referral" | "regular" | null;

export interface Job {
  id: number;
  job_id: string;
  title: string;
  company: string;
  location: string | null;
  description: string | null;
  apply_url: string | null;
  date_posted: string | null;
  salary: string | null;
  source: JobSource;
  // Scoring
  score: number | null;
  reason: string | null;
  level: JobLevel | null;
  role_type: string | null;
  tech_stack_match: string[];
  is_student_position: boolean;
  apply_strategy: string | null;
  role_summary: string | null;
  requirements_summary: string | null;
  // Lifecycle
  status: JobStatus;
  cover_letter_used: string | null;
  error_message: string | null;
  // Dashboard fields
  notes: string | null;
  referral_type: ReferralType;
  referral_url: string | null;
  // Timestamps
  found_at: string | null;
  notified_at: string | null;
  applied_at: string | null;
  status_updated_at: string | null;
}

export interface Stats {
  total: number;
  by_status: Record<string, number>;
  by_level: Record<string, number>;
  by_source: Record<string, number>;
  recent: Job[];
}

export interface JobsResponse {
  jobs: Job[];
  total: number;
  page: number;
  per_page: number;
}
