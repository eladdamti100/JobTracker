import type { JobStatus } from "@/types";

interface StatusConfig {
  label: string;
  className: string;
}

export const STATUS_CONFIG: Record<string, StatusConfig> = {
  new:        { label: "New",        className: "bg-gray-100 text-gray-700 border-gray-200" },
  scored:     { label: "Scored",     className: "bg-slate-100 text-slate-700 border-slate-200" },
  notified:   { label: "Notified",   className: "bg-purple-50 text-purple-700 border-purple-200" },
  approved:   { label: "Approved",   className: "bg-amber-50 text-amber-700 border-amber-200" },
  applying:   { label: "Applying",   className: "bg-orange-50 text-orange-700 border-orange-200" },
  applied:    { label: "Applied",    className: "bg-blue-100 text-blue-800 border-blue-200" },
  failed:     { label: "Failed",     className: "bg-red-50 text-red-700 border-red-200" },
  in_review:  { label: "In Review",  className: "bg-violet-100 text-violet-700 border-violet-200" },
  rejected:   { label: "Rejected",   className: "bg-red-100 text-red-800 border-red-300" },
  interview:  { label: "Interview",  className: "bg-indigo-100 text-indigo-700 border-indigo-200" },
  next_stage: { label: "Next Stage", className: "bg-cyan-100 text-cyan-700 border-cyan-200" },
  accepted:   { label: "Accepted",   className: "bg-emerald-100 text-emerald-700 border-emerald-200" },
};

export const USER_STATUSES: JobStatus[] = [
  "applied", "in_review", "rejected", "interview", "next_stage", "accepted",
];

interface Props {
  status: string;
  size?: "sm" | "md";
}

export default function StatusBadge({ status, size = "md" }: Props) {
  const config = STATUS_CONFIG[status] ?? { label: status, className: "bg-gray-100 text-gray-600 border-gray-200" };
  const sizeClass = size === "sm" ? "px-2 py-0.5 text-xs" : "px-2.5 py-1 text-xs font-medium";
  return (
    <span className={`inline-flex items-center rounded-full border ${sizeClass} ${config.className}`}>
      {config.label}
    </span>
  );
}
