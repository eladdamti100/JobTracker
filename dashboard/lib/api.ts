import type { Job, JobsResponse, Stats } from "@/types";

const API_URL =
  process.env.NEXT_PUBLIC_API_URL ??
  (typeof window !== "undefined" ? window.location.origin : "http://localhost:5001");

function buildQuery(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  return q.toString();
}

export async function fetchJobs(params: {
  page?: number;
  per_page?: number;
  status?: string;
  source?: string;
  level?: string;
  referral_type?: string;
  search?: string;
  sort?: string;
  order?: string;
}): Promise<JobsResponse> {
  const res = await fetch(`${API_URL}/api/jobs?${buildQuery(params)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Failed to fetch jobs: ${res.status}`);
  return res.json();
}

export async function fetchJob(jobId: string): Promise<Job> {
  const res = await fetch(`${API_URL}/api/jobs/${jobId}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Job not found: ${res.status}`);
  return res.json();
}

export async function fetchStats(): Promise<Stats> {
  const res = await fetch(`${API_URL}/api/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to fetch stats: ${res.status}`);
  return res.json();
}

export async function updateJob(
  jobId: string,
  data: Partial<Pick<Job, "status" | "notes" | "referral_type" | "referral_url">>
): Promise<Job> {
  const res = await fetch(`${API_URL}/api/jobs/${jobId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Failed to update job: ${res.status}`);
  return res.json();
}
