"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { Search, ChevronUp, ChevronDown, ExternalLink } from "lucide-react";
import { fetchJobs } from "@/lib/api";
import StatusBadge, { STATUS_CONFIG } from "@/components/StatusBadge";
import type { Job, JobStatus } from "@/types";

const SOURCES = ["HireMeTech", "LinkedIn", "WhatsApp"];
const LEVELS = ["student", "junior", "senior"];
const REFERRAL_TYPES = ["referral", "regular"];

function ScoreDot({ score }: { score: number | null }) {
  if (score === null) return <span className="text-gray-300">—</span>;
  const color =
    score >= 8 ? "text-emerald-600" : score >= 6 ? "text-amber-600" : "text-red-500";
  return <span className={`font-semibold ${color}`}>{score.toFixed(1)}</span>;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

type SortKey = "found_at" | "score" | "company" | "applied_at";

export default function ApplicationsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [level, setLevel] = useState("");
  const [referralType, setReferralType] = useState("");
  const [sort, setSort] = useState<SortKey>("found_at");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);

  const PER_PAGE = 20;

  // Debounce search
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchJobs({
        search: debouncedSearch || undefined,
        status: status || undefined,
        source: source || undefined,
        level: level || undefined,
        referral_type: referralType || undefined,
        sort,
        order,
        page,
        per_page: PER_PAGE,
      });
      setJobs(data.jobs);
      setTotal(data.total);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [debouncedSearch, status, source, level, referralType, sort, order, page]);

  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, status, source, level, referralType]);

  useEffect(() => {
    load();
  }, [load]);

  function toggleSort(key: SortKey) {
    if (sort === key) {
      setOrder((o) => (o === "desc" ? "asc" : "desc"));
    } else {
      setSort(key);
      setOrder("desc");
    }
  }

  function SortIcon({ col }: { col: SortKey }) {
    if (sort !== col) return <ChevronUp className="w-3 h-3 text-gray-300" />;
    return order === "asc" ? (
      <ChevronUp className="w-3 h-3 text-indigo-500" />
    ) : (
      <ChevronDown className="w-3 h-3 text-indigo-500" />
    );
  }

  const totalPages = Math.ceil(total / PER_PAGE);

  return (
    <div className="p-8 max-w-7xl mx-auto">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Applications</h1>
        <p className="text-gray-500 text-sm mt-1">{total} jobs tracked</p>
      </div>

      {/* Filters bar */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 mb-6 shadow-sm">
        <div className="flex flex-wrap gap-3">
          {/* Search */}
          <div className="relative flex-1 min-w-48">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search company or title…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-9 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-300 bg-gray-50"
            />
          </div>

          {/* Status filter */}
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 bg-gray-50 focus:outline-none focus:ring-2 focus:ring-indigo-300"
          >
            <option value="">All Statuses</option>
            {Object.entries(STATUS_CONFIG).map(([val, { label }]) => (
              <option key={val} value={val}>{label}</option>
            ))}
          </select>

          {/* Source filter */}
          <select
            value={source}
            onChange={(e) => setSource(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 bg-gray-50 focus:outline-none focus:ring-2 focus:ring-indigo-300"
          >
            <option value="">All Sources</option>
            {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>

          {/* Level filter */}
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 bg-gray-50 focus:outline-none focus:ring-2 focus:ring-indigo-300"
          >
            <option value="">All Levels</option>
            {LEVELS.map((l) => (
              <option key={l} value={l}>{l.charAt(0).toUpperCase() + l.slice(1)}</option>
            ))}
          </select>

          {/* Referral filter */}
          <select
            value={referralType}
            onChange={(e) => setReferralType(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 bg-gray-50 focus:outline-none focus:ring-2 focus:ring-indigo-300"
          >
            <option value="">All Types</option>
            {REFERRAL_TYPES.map((r) => (
              <option key={r} value={r}>{r.charAt(0).toUpperCase() + r.slice(1)}</option>
            ))}
          </select>

          {/* Reset */}
          {(search || status || source || level || referralType) && (
            <button
              onClick={() => { setSearch(""); setStatus(""); setSource(""); setLevel(""); setReferralType(""); }}
              className="text-sm text-gray-400 hover:text-gray-600 px-3 py-2"
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {error ? (
          <div className="p-8 text-center text-red-500 text-sm">{error}</div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-100">
                  <tr>
                    <th
                      className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide cursor-pointer hover:text-gray-600 select-none"
                      onClick={() => toggleSort("company")}
                    >
                      <span className="flex items-center gap-1">Company <SortIcon col="company" /></span>
                    </th>
                    <th className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide">Title</th>
                    <th className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide">Location</th>
                    <th className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide">Source</th>
                    <th className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide">Level</th>
                    <th
                      className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide cursor-pointer hover:text-gray-600 select-none"
                      onClick={() => toggleSort("score")}
                    >
                      <span className="flex items-center gap-1">Score <SortIcon col="score" /></span>
                    </th>
                    <th className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide">Status</th>
                    <th
                      className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide cursor-pointer hover:text-gray-600 select-none"
                      onClick={() => toggleSort("found_at")}
                    >
                      <span className="flex items-center gap-1">Found <SortIcon col="found_at" /></span>
                    </th>
                    <th className="px-5 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wide">Apply</th>
                  </tr>
                </thead>
                <tbody className={loading ? "opacity-50" : ""}>
                  {jobs.length === 0 && !loading ? (
                    <tr>
                      <td colSpan={9} className="px-5 py-12 text-center text-gray-400">
                        No jobs found. Adjust filters or run a scan.
                      </td>
                    </tr>
                  ) : (
                    jobs.map((job, i) => (
                      <tr
                        key={job.job_id}
                        className={`hover:bg-indigo-50/30 transition-colors border-b border-gray-50 ${i % 2 === 1 ? "bg-gray-50/30" : ""}`}
                      >
                        <td className="px-5 py-3 font-medium text-gray-800">
                          <Link href={`/applications/${job.job_id}`} className="hover:text-indigo-700">
                            {job.company}
                          </Link>
                        </td>
                        <td className="px-5 py-3 text-gray-600 max-w-xs truncate">
                          <Link href={`/applications/${job.job_id}`} className="hover:text-indigo-600">
                            {job.title}
                          </Link>
                        </td>
                        <td className="px-5 py-3 text-gray-400 text-xs">{job.location ?? "—"}</td>
                        <td className="px-5 py-3 text-gray-500">{job.source}</td>
                        <td className="px-5 py-3">
                          {job.level ? (
                            <span className="text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 capitalize">
                              {job.level}
                            </span>
                          ) : "—"}
                        </td>
                        <td className="px-5 py-3"><ScoreDot score={job.score} /></td>
                        <td className="px-5 py-3"><StatusBadge status={job.status} /></td>
                        <td className="px-5 py-3 text-gray-400 text-xs whitespace-nowrap">
                          {formatDate(job.found_at)}
                        </td>
                        <td className="px-5 py-3">
                          {job.apply_url ? (
                            <a
                              href={job.apply_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-indigo-500 hover:text-indigo-700"
                            >
                              <ExternalLink className="w-3.5 h-3.5" />
                            </a>
                          ) : "—"}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-between">
                <span className="text-xs text-gray-400">
                  Page {page} of {totalPages} · {total} total
                </span>
                <div className="flex gap-2">
                  <button
                    disabled={page === 1}
                    onClick={() => setPage((p) => p - 1)}
                    className="px-3 py-1.5 text-xs rounded-lg border border-gray-200 disabled:opacity-40 hover:bg-gray-50"
                  >
                    Prev
                  </button>
                  <button
                    disabled={page >= totalPages}
                    onClick={() => setPage((p) => p + 1)}
                    className="px-3 py-1.5 text-xs rounded-lg border border-gray-200 disabled:opacity-40 hover:bg-gray-50"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
