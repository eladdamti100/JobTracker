interface StatusConfig {
  label: string;
  className: string;
}

export const STATUS_CONFIG: Record<string, StatusConfig> = {
  // Suggested job statuses
  suggested:  { label: "Pending",   className: "bg-yellow-50 text-yellow-700 border-yellow-200" },
  approved:   { label: "Approved",  className: "bg-amber-50 text-amber-700 border-amber-200" },
  rejected:   { label: "Rejected",  className: "bg-red-100 text-red-800 border-red-300" },
  skipped:    { label: "Skipped",   className: "bg-gray-100 text-gray-600 border-gray-200" },
  expired:    { label: "Expired",   className: "bg-slate-100 text-slate-500 border-slate-200" },
  applied:    { label: "Applied",   className: "bg-blue-100 text-blue-800 border-blue-200" },
  // Application statuses
  success:    { label: "Success",   className: "bg-emerald-100 text-emerald-700 border-emerald-200" },
  failed:     { label: "Failed",    className: "bg-red-50 text-red-700 border-red-200" },
  pending:    { label: "Pending",   className: "bg-orange-50 text-orange-700 border-orange-200" },
};

export const SUGGESTED_STATUSES = ["suggested", "approved", "rejected", "skipped", "expired", "applied"] as const;
export const APPLICATION_STATUSES = ["success", "failed", "pending"] as const;

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
