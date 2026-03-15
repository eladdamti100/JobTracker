import Link from "next/link";
import { Briefcase, CheckCircle, Inbox, Clock, ExternalLink } from "lucide-react";
import { fetchStats } from "@/lib/api";
import StatsCard from "@/components/StatsCard";
import StatusBadge from "@/components/StatusBadge";
import type { Stats } from "@/types";

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default async function DashboardPage() {
  let stats: Stats | null = null;
  let error: string | null = null;

  try {
    stats = await fetchStats();
  } catch (e) {
    error = String(e);
  }

  if (error || !stats) {
    return (
      <div className="flex items-center justify-center h-full min-h-[60vh]">
        <div className="text-center">
          <p className="text-red-500 font-medium">Cannot reach API</p>
          <p className="text-gray-500 text-sm mt-1">
            Start the Python API: <code className="bg-gray-100 px-1 rounded">python main.py api</code>
          </p>
          {error && <p className="text-gray-400 text-xs mt-2">{error}</p>}
        </div>
      </div>
    );
  }

  const suggestedStatus = stats.suggested.by_status;
  const appStatus = stats.applications.by_status;

  const pendingSuggestions = suggestedStatus.suggested ?? 0;
  const totalSuggested = stats.suggested.total;
  const totalApplied = stats.applications.total;
  const successCount = appStatus.success ?? 0;
  const failedCount = appStatus.failed ?? 0;

  const sourceColors: Record<string, string> = {
    LinkedIn: "bg-blue-500",
    HireMeTech: "bg-indigo-500",
    WhatsApp: "bg-emerald-500",
    Unknown: "bg-gray-400",
  };

  const suggestedStatusOrder = ["suggested", "approved", "rejected", "skipped", "expired", "applied"];

  return (
    <div className="p-8 max-w-6xl mx-auto">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
        <p className="text-gray-500 text-sm mt-1">
          {new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}
        </p>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <StatsCard
          label="Suggested"
          value={totalSuggested}
          sub="total jobs found"
          accent="bg-indigo-50"
          icon={<Inbox className="w-5 h-5 text-indigo-600" />}
        />
        <StatsCard
          label="Pending Reply"
          value={pendingSuggestions}
          sub="awaiting YES/NO"
          accent="bg-yellow-50"
          icon={<Clock className="w-5 h-5 text-yellow-600" />}
        />
        <StatsCard
          label="Applications"
          value={totalApplied}
          sub={`${successCount} success / ${failedCount} failed`}
          accent="bg-blue-50"
          icon={<CheckCircle className="w-5 h-5 text-blue-600" />}
        />
        <StatsCard
          label="Success Rate"
          value={totalApplied > 0 ? `${Math.round((successCount / totalApplied) * 100)}%` : "—"}
          sub="of applications"
          accent="bg-emerald-50"
          icon={<Briefcase className="w-5 h-5 text-emerald-600" />}
        />
      </div>

      {/* Source + Suggestion Status breakdown */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        {/* Source breakdown */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Source Breakdown</h2>
          <div className="space-y-3">
            {Object.entries(stats.suggested.by_source).map(([source, count]) => {
              const pct = totalSuggested > 0 ? Math.round((count / totalSuggested) * 100) : 0;
              return (
                <div key={source}>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="text-gray-700">{source}</span>
                    <span className="text-gray-500">{count} <span className="text-gray-400 text-xs">({pct}%)</span></span>
                  </div>
                  <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full ${sourceColors[source] ?? "bg-gray-400"}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              );
            })}
            {Object.keys(stats.suggested.by_source).length === 0 && (
              <p className="text-gray-400 text-sm">No data yet</p>
            )}
          </div>
        </div>

        {/* Suggestion status breakdown */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Suggestion Status</h2>
          <div className="grid grid-cols-2 gap-2">
            {suggestedStatusOrder
              .filter((st) => suggestedStatus[st])
              .map((st) => (
                <div key={st} className="flex items-center justify-between p-2 rounded-lg bg-gray-50">
                  <StatusBadge status={st} size="sm" />
                  <span className="text-sm font-semibold text-gray-700">{suggestedStatus[st]}</span>
                </div>
              ))}
            {Object.keys(suggestedStatus).length === 0 && (
              <p className="text-gray-400 text-sm col-span-2">No data yet</p>
            )}
          </div>
        </div>
      </div>

      {/* Level breakdown */}
      {Object.keys(stats.suggested.by_level).length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm mb-8">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Level Breakdown</h2>
          <div className="flex gap-4">
            {Object.entries(stats.suggested.by_level).map(([level, count]) => (
              <div key={level} className="flex-1 text-center p-3 rounded-lg bg-gray-50">
                <p className="text-2xl font-bold text-gray-800">{count}</p>
                <p className="text-xs text-gray-500 capitalize mt-0.5">{level}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Two sections side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Pending Suggestions */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm">
          <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
            <h2 className="text-sm font-semibold text-gray-700">Pending Suggestions</h2>
            <Link href="/suggested" className="text-xs text-indigo-600 hover:text-indigo-800 font-medium">
              View all →
            </Link>
          </div>
          {stats.suggested.pending.length === 0 ? (
            <div className="px-5 py-10 text-center text-gray-400 text-sm">
              No pending suggestions. Run <code className="bg-gray-100 px-1 rounded">python main.py scan</code> to find jobs.
            </div>
          ) : (
            <div className="divide-y divide-gray-50">
              {stats.suggested.pending.map((job) => (
                <div key={job.job_hash} className="px-5 py-3 flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-800">{job.company}</p>
                    <p className="text-xs text-gray-500 truncate max-w-[200px]">{job.title}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    {job.score !== null && (
                      <span className={`text-xs font-semibold ${job.score >= 8 ? "text-emerald-600" : "text-amber-600"}`}>
                        {job.score.toFixed(1)}
                      </span>
                    )}
                    <StatusBadge status={job.status} size="sm" />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Recent Applications */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm">
          <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
            <h2 className="text-sm font-semibold text-gray-700">Recent Applications</h2>
            <Link href="/applications" className="text-xs text-indigo-600 hover:text-indigo-800 font-medium">
              View all →
            </Link>
          </div>
          {stats.applications.recent.length === 0 ? (
            <div className="px-5 py-10 text-center text-gray-400 text-sm">
              No applications yet. Approve suggested jobs to start applying.
            </div>
          ) : (
            <div className="divide-y divide-gray-50">
              {stats.applications.recent.map((app) => (
                <div key={`${app.job_hash}-${app.id}`} className="px-5 py-3 flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-800">{app.company}</p>
                    <p className="text-xs text-gray-500 truncate max-w-[200px]">{app.title}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-gray-400">{formatDate(app.applied_at)}</span>
                    <StatusBadge status={app.status} size="sm" />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
