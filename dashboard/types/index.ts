// Suggested Jobs
export type SuggestedStatus =
  | "suggested"
  | "approved"
  | "rejected"
  | "skipped"
  | "expired"
  | "applied";

export type JobLevel = "student" | "junior" | "senior";
export type JobSource = "HireMeTech" | "LinkedIn" | "WhatsApp" | "Unknown";

// Applications
export type ApplicationStatus = "success" | "failed" | "pending";

export interface SuggestedJob {
  id: number;
  job_hash: string;
  company: string;
  title: string;
  source: JobSource | string;
  apply_url: string | null;
  location: string | null;
  description: string | null;
  date_posted: string | null;
  salary: string | null;
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
  status: SuggestedStatus;
  // Timestamps
  created_at: string | null;
  expires_at: string | null;
  responded_at: string | null;
}

export interface Application {
  id: number;
  job_hash: string;
  company: string;
  title: string;
  source: string | null;
  apply_url: string | null;
  // Application details
  applied_at: string | null;
  application_method: string | null;
  application_result: string | null;
  status: ApplicationStatus;
  // Evidence
  screenshot_path: string | null;
  cover_letter_used: string | null;
  error_message: string | null;
}

export interface SuggestedJobsResponse {
  jobs: SuggestedJob[];
  total: number;
  page: number;
  per_page: number;
}

export interface ApplicationsResponse {
  applications: Application[];
  total: number;
  page: number;
  per_page: number;
}

export interface Stats {
  suggested: {
    total: number;
    by_status: Record<string, number>;
    by_level: Record<string, number>;
    by_source: Record<string, number>;
    pending: SuggestedJob[];
  };
  applications: {
    total: number;
    by_status: Record<string, number>;
    recent: Application[];
  };
}
