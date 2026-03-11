"use client";

import { useState } from "react";
import { Check, Loader2 } from "lucide-react";
import { updateJob } from "@/lib/api";
import StatusBadge, { USER_STATUSES, STATUS_CONFIG } from "@/components/StatusBadge";
import type { Job, JobStatus, ReferralType } from "@/types";

interface Props {
  job: Job;
}

export default function ApplicationActions({ job }: Props) {
  const [status, setStatus] = useState<JobStatus>(job.status);
  const [notes, setNotes] = useState(job.notes ?? "");
  const [referralType, setReferralType] = useState<ReferralType>(job.referral_type);
  const [referralUrl, setReferralUrl] = useState(job.referral_url ?? "");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  async function save() {
    setSaving(true);
    setSaved(false);
    try {
      await updateJob(job.job_id, {
        status,
        notes: notes || undefined,
        referral_type: referralType ?? undefined,
        referral_url: referralUrl || undefined,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  const dirty =
    status !== job.status ||
    notes !== (job.notes ?? "") ||
    referralType !== job.referral_type ||
    referralUrl !== (job.referral_url ?? "");

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <h2 className="text-sm font-semibold text-gray-700 mb-4">Application Tracking</h2>

      {/* Status selector */}
      <div className="mb-4">
        <label className="block text-xs text-gray-400 mb-2">Status</label>
        <div className="flex flex-wrap gap-1.5">
          {USER_STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={`text-xs px-2.5 py-1 rounded-full border transition-all ${
                status === s
                  ? STATUS_CONFIG[s]?.className + " ring-1 ring-offset-1 ring-indigo-400"
                  : "bg-gray-50 text-gray-500 border-gray-200 hover:bg-gray-100"
              }`}
            >
              {STATUS_CONFIG[s]?.label ?? s}
            </button>
          ))}
        </div>
        {/* Show current internal status if it's not a user status */}
        {!USER_STATUSES.includes(status) && (
          <div className="mt-2">
            <StatusBadge status={status} size="sm" />
          </div>
        )}
      </div>

      {/* Referral type */}
      <div className="mb-4">
        <label className="block text-xs text-gray-400 mb-2">Application Type</label>
        <div className="flex gap-2">
          {(["referral", "regular"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setReferralType(referralType === t ? null : t)}
              className={`text-xs px-3 py-1.5 rounded-lg border transition-all capitalize ${
                referralType === t
                  ? "bg-indigo-50 text-indigo-700 border-indigo-200"
                  : "bg-gray-50 text-gray-500 border-gray-200 hover:bg-gray-100"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {/* Referral URL */}
      {referralType === "referral" && (
        <div className="mb-4">
          <label className="block text-xs text-gray-400 mb-1.5">Referral URL</label>
          <input
            type="url"
            value={referralUrl}
            onChange={(e) => setReferralUrl(e.target.value)}
            placeholder="https://…"
            className="w-full text-xs border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300 bg-gray-50"
          />
        </div>
      )}

      {/* Notes */}
      <div className="mb-4">
        <label className="block text-xs text-gray-400 mb-1.5">Notes</label>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Add notes, contacts, follow-up dates…"
          rows={3}
          className="w-full text-xs border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300 bg-gray-50 resize-none"
        />
      </div>

      {/* Save button */}
      <button
        onClick={save}
        disabled={saving || (!dirty && !saved)}
        className={`w-full py-2 rounded-lg text-sm font-medium transition-all flex items-center justify-center gap-1.5 ${
          saved
            ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
            : dirty
            ? "bg-indigo-600 text-white hover:bg-indigo-700"
            : "bg-gray-100 text-gray-400 cursor-not-allowed"
        }`}
      >
        {saving ? (
          <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Saving…</>
        ) : saved ? (
          <><Check className="w-3.5 h-3.5" /> Saved</>
        ) : (
          "Save Changes"
        )}
      </button>
    </div>
  );
}
