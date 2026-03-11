import Link from "next/link";
import { Briefcase, CheckCircle, Calendar, TrendingUp, ExternalLink } from "lucide-react";
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

  const s = stats.by_status;
  const totalApplied =
    (s.applied ?? 0) + (s.in_review ?? 0) + (s.rejected ?? 0) +
    (s.interview ?? 0) + (s.next_stage ?? 0) + (s.accepted ?? 0);
  const interviews = (s.interview ?? 0) + (s.next_stage ?? 0);
  const accepted = s.accepted ?? 0;

  const sourceColors: Record<string, string> = {
    LinkedIn: "bg-blue-500",
    HireMeTech: "bg-indigo-500",
    WhatsApp: "bg-emerald-500",
    Unknown: "bg-gray-400",
  };

  const statusDisplayOrder = [
    "new", "scored", "notified", "approved", "applying",
    "applied", "in_review", "rejected", "interview", "next_stage", "accepted", "failed",
  ];

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
          label="Total Tracked"
          value={stats.total}
          sub="all sources"
          accent="bg-indigo-50"
          icon={<Briefcase className="w-5 h-5 text-indigo-600" />}
        />
        <StatsCard
          label="Applications"
          value={totalApplied}
          sub="submitted"
          accent="bg-blue-50"
          icon={<CheckCircle className="w-5 h-5 text-blue-600" />}
        />
        <StatsCard
          label="Interviews"
          value={interviews}
          sub="scheduled / next stage"
          accent="bg-amber-50"
          icon={<Calendar className="w-5 h-5 text-amber-600" />}
        />
        <StatsCard
          label="Accepted"
          value={accepted}
          sub="offers received"
          accent="bg-emerald-50"
          icon={<TrendingUp className="w-5 h-5 text-emerald-600" />}
        />
      </div>

      {/* Source + Status breakdown */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        {/* Source breakdown */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Source Breakdown</h2>
          <div className="space-y-3">
            {Object.entries(stats.by_source).map(([source, count]) => {
              const pct = stats.total > 0 ? Math.round((count / stats.total) * 100) : 0;
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
            {Object.keys(stats.by_source).length === 0 && (
              <p className="text-gray-400 text-sm">No data yet</p>
            )}
          </div>
        </div>

        {/* Status breakdown */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Status Breakdown</h2>
          <div className="grid grid-cols-2 gap-2">
            {statusDisplayOrder
              .filter((st) => s[st])
              .map((st) => (
                <div key={st} className="flex items-center justify-between p-2 rounded-lg bg-gray-50">
                  <StatusBadge status={st} size="sm" />
                  <span className="text-sm font-semibold text-gray-700">{s[st]}</span>
                </div>
              ))}
            {Object.keys(s).length === 0 && (
              <p className="text-gray-400 text-sm col-span-2">No data yet</p>
            )}
          </div>
        </div>
      </div>

      {/* Level breakdown */}
      {Object.keys(stats.by_level).length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm mb-8">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Level Breakdown</h2>
          <div className="flex gap-4">
            {Object.entries(stats.by_level).map(([level, count]) => (
              <div key={level} className="flex-1 text-center p-3 rounded-lg bg-gray-50">
                <p className="text-2xl font-bold text-gray-800">{count}</p>
                <p className="text-xs text-gray-500 capitalize mt-0.5">{level}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Recent applications */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="text-sm font-semibold text-gray-700">Recent Applications</h2>
          <Link href="/applications" className="text-xs text-indigo-600 hover:text-indigo-800 font-medium">
            View all →
          </Link>
        </div>
        {stats.recent.length === 0 ? (
          <div className="px-5 py-10 text-center text-gray-400 text-sm">
            No applications yet. Run <code className="bg-gray-100 px-1 rounded">python main.py scan</code> to get started.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-400 uppercase tracking-wide">
                <th className="px-5 py-3 text-left font-medium">Company</th>
                <th className="px-5 py-3 text-left font-medium">Title</th>
                <th className="px-5 py-3 text-left font-medium">Source</th>
                <th className="px-5 py-3 text-left font-medium">Status</th>
                <th className="px-5 py-3 text-left font-medium">Date</th>
                <th className="px-5 py-3 text-left font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {stats.recent.map((job, i) => (
                <tr key={job.job_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-5 py-3 font-medium text-gray-800">{job.company}</td>
                  <td className="px-5 py-3 text-gray-600">{job.title}</td>
                  <td className="px-5 py-3 text-gray-500">{job.source}</td>
                  <td className="px-5 py-3"><StatusBadge status={job.status} /></td>
                  <td className="px-5 py-3 text-gray-400">{formatDate(job.applied_at ?? job.found_at)}</td>
                  <td className="px-5 py-3">
                    <Link href={`/applications/${job.job_id}`} className="text-indigo-500 hover:text-indigo-700">
                      <ExternalLink className="w-3.5 h-3.5" />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
