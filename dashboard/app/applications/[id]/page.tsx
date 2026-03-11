import { notFound } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, ExternalLink, MapPin, Building2, Calendar, Star, Zap } from "lucide-react";
import { fetchJob } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import ApplicationActions from "./ApplicationActions";

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric", month: "long", day: "numeric",
  });
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

interface Props {
  params: Promise<{ id: string }>;
}

export default async function ApplicationDetailPage({ params }: Props) {
  const { id } = await params;

  let job;
  try {
    job = await fetchJob(id);
  } catch {
    notFound();
  }

  const techStack: string[] = Array.isArray(job.tech_stack_match)
    ? job.tech_stack_match
    : [];

  return (
    <div className="p-8 max-w-5xl mx-auto">
      {/* Back */}
      <Link
        href="/applications"
        className="inline-flex items-center gap-1.5 text-sm text-gray-400 hover:text-gray-700 mb-6 transition-colors"
      >
        <ArrowLeft className="w-4 h-4" />
        Back to Applications
      </Link>

      {/* Header */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm mb-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">
                {job.job_id}
              </span>
              <span className="text-xs text-gray-400">{job.source}</span>
            </div>
            <h1 className="text-xl font-bold text-gray-900">{job.title}</h1>
            <div className="flex items-center gap-4 mt-2 text-sm text-gray-500">
              <span className="flex items-center gap-1">
                <Building2 className="w-3.5 h-3.5" /> {job.company}
              </span>
              {job.location && (
                <span className="flex items-center gap-1">
                  <MapPin className="w-3.5 h-3.5" /> {job.location}
                </span>
              )}
              {job.date_posted && (
                <span className="flex items-center gap-1">
                  <Calendar className="w-3.5 h-3.5" /> Posted {job.date_posted}
                </span>
              )}
            </div>
          </div>
          <div className="flex flex-col items-end gap-2">
            <StatusBadge status={job.status} />
            {job.score !== null && (
              <div className="flex items-center gap-1">
                <Star className="w-3.5 h-3.5 text-amber-400 fill-amber-400" />
                <span className="text-sm font-semibold text-amber-700">{job.score.toFixed(1)}/10</span>
              </div>
            )}
          </div>
        </div>

        {job.apply_url && (
          <div className="mt-4 pt-4 border-t border-gray-100">
            <a
              href={job.apply_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm text-indigo-600 hover:text-indigo-800 font-medium"
            >
              Apply Now <ExternalLink className="w-3.5 h-3.5" />
            </a>
            <span className="ml-2 text-xs text-gray-400 font-mono truncate max-w-xs inline-block align-bottom">
              {job.apply_url}
            </span>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: Job details */}
        <div className="lg:col-span-2 space-y-6">
          {/* AI Scoring */}
          {(job.role_summary || job.requirements_summary || job.reason) && (
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="flex items-center gap-2 mb-4">
                <Zap className="w-4 h-4 text-indigo-500" />
                <h2 className="text-sm font-semibold text-gray-700">AI Analysis</h2>
              </div>
              {job.role_summary && (
                <div className="mb-3">
                  <p className="text-xs text-gray-400 uppercase tracking-wide mb-1">Role Summary</p>
                  <p className="text-sm text-gray-700 leading-relaxed">{job.role_summary}</p>
                </div>
              )}
              {job.requirements_summary && (
                <div className="mb-3">
                  <p className="text-xs text-gray-400 uppercase tracking-wide mb-1">Requirements</p>
                  <p className="text-sm text-gray-700 leading-relaxed">{job.requirements_summary}</p>
                </div>
              )}
              {job.reason && (
                <div>
                  <p className="text-xs text-gray-400 uppercase tracking-wide mb-1">Scoring Reason</p>
                  <p className="text-sm text-gray-600 italic">{job.reason}</p>
                </div>
              )}
              {techStack.length > 0 && (
                <div className="mt-3 pt-3 border-t border-gray-100">
                  <p className="text-xs text-gray-400 uppercase tracking-wide mb-2">Matching Tech Stack</p>
                  <div className="flex flex-wrap gap-1.5">
                    {techStack.map((tech) => (
                      <span key={tech} className="text-xs px-2 py-1 bg-indigo-50 text-indigo-700 rounded-md">
                        {tech}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Job description */}
          {job.description && (
            <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <h2 className="text-sm font-semibold text-gray-700 mb-3">Job Description</h2>
              <div className="text-sm text-gray-600 whitespace-pre-wrap leading-relaxed max-h-80 overflow-y-auto scrollbar-thin">
                {job.description}
              </div>
            </div>
          )}
        </div>

        {/* Right: Application tracking */}
        <div className="space-y-6">
          {/* Metadata */}
          <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-gray-700 mb-4">Job Info</h2>
            <dl className="space-y-2.5 text-sm">
              <div className="flex justify-between">
                <dt className="text-gray-400">Level</dt>
                <dd className="text-gray-700 capitalize">{job.level ?? "—"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-400">Role type</dt>
                <dd className="text-gray-700">{job.role_type ?? "—"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-400">Strategy</dt>
                <dd className="text-gray-700">{job.apply_strategy ?? "—"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-400">Student pos.</dt>
                <dd className="text-gray-700">{job.is_student_position ? "Yes" : "No"}</dd>
              </div>
              {job.salary && (
                <div className="flex justify-between">
                  <dt className="text-gray-400">Salary</dt>
                  <dd className="text-gray-700">{job.salary}</dd>
                </div>
              )}
            </dl>
          </div>

          {/* Timestamps */}
          <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-gray-700 mb-4">Timeline</h2>
            <dl className="space-y-2.5 text-sm">
              <div>
                <dt className="text-gray-400 text-xs">Found</dt>
                <dd className="text-gray-700">{formatDate(job.found_at)}</dd>
              </div>
              <div>
                <dt className="text-gray-400 text-xs">Notified</dt>
                <dd className="text-gray-700">{formatDateTime(job.notified_at)}</dd>
              </div>
              <div>
                <dt className="text-gray-400 text-xs">Applied</dt>
                <dd className="text-gray-700">{formatDate(job.applied_at)}</dd>
              </div>
              <div>
                <dt className="text-gray-400 text-xs">Status updated</dt>
                <dd className="text-gray-700">{formatDateTime(job.status_updated_at)}</dd>
              </div>
            </dl>
          </div>

          {/* Interactive: status + notes + referral */}
          <ApplicationActions job={job} />
        </div>
      </div>
    </div>
  );
}
