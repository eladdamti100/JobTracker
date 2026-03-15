import type {
  SuggestedJob,
  SuggestedJobsResponse,
  Application,
  ApplicationsResponse,
  Stats,
} from "@/types";

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

// --- Suggested Jobs ---

export async function fetchSuggested(params: {
  page?: number;
  per_page?: number;
  status?: string;
  source?: string;
  level?: string;
  search?: string;
  sort?: string;
  order?: string;
}): Promise<SuggestedJobsResponse> {
  const res = await fetch(`${API_URL}/api/suggested?${buildQuery(params)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Failed to fetch suggested jobs: ${res.status}`);
  return res.json();
}

export async function fetchSuggestedJob(jobHash: string): Promise<SuggestedJob> {
  const res = await fetch(`${API_URL}/api/suggested/${jobHash}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Suggested job not found: ${res.status}`);
  return res.json();
}

export async function updateSuggested(
  jobHash: string,
  data: { status: string }
): Promise<SuggestedJob> {
  const res = await fetch(`${API_URL}/api/suggested/${jobHash}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Failed to update suggested job: ${res.status}`);
  return res.json();
}

// --- Applications ---

export async function fetchApplications(params: {
  page?: number;
  per_page?: number;
  status?: string;
  source?: string;
  search?: string;
  sort?: string;
  order?: string;
}): Promise<ApplicationsResponse> {
  const res = await fetch(`${API_URL}/api/applications?${buildQuery(params)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Failed to fetch applications: ${res.status}`);
  return res.json();
}

export async function fetchApplication(jobHash: string): Promise<Application> {
  const res = await fetch(`${API_URL}/api/applications/${jobHash}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Application not found: ${res.status}`);
  return res.json();
}

// --- Stats ---

export async function fetchStats(): Promise<Stats> {
  const res = await fetch(`${API_URL}/api/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to fetch stats: ${res.status}`);
  return res.json();
}
