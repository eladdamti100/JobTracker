"use client";

import { useState } from "react";
import { Check, X, Loader2 } from "lucide-react";
import { updateSuggested } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import type { SuggestedJob } from "@/types";

interface Props {
  job: SuggestedJob;
}

export default function SuggestedActions({ job }: Props) {
  const [currentStatus, setCurrentStatus] = useState(job.status);
  const [saving, setSaving] = useState(false);

  async function handleAction(newStatus: string) {
    setSaving(true);
    try {
      const updated = await updateSuggested(job.job_hash, { status: newStatus });
      setCurrentStatus(updated.status);
    } catch (e) {
      alert(`Failed: ${e}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <h2 className="text-sm font-semibold text-gray-700 mb-4">Actions</h2>

      <div className="mb-4">
        <p className="text-xs text-gray-400 mb-2">Current status</p>
        <StatusBadge status={currentStatus} />
      </div>

      {currentStatus === "suggested" && (
        <div className="space-y-2">
          <button
            onClick={() => handleAction("approved")}
            disabled={saving}
            className="w-full py-2.5 rounded-lg text-sm font-medium bg-emerald-600 text-white hover:bg-emerald-700 transition-colors flex items-center justify-center gap-1.5 disabled:opacity-50"
          >
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
            Approve & Apply
          </button>
          <button
            onClick={() => handleAction("rejected")}
            disabled={saving}
            className="w-full py-2.5 rounded-lg text-sm font-medium bg-red-50 text-red-700 border border-red-200 hover:bg-red-100 transition-colors flex items-center justify-center gap-1.5 disabled:opacity-50"
          >
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <X className="w-3.5 h-3.5" />}
            Reject
          </button>
        </div>
      )}

      {currentStatus !== "suggested" && (
        <p className="text-xs text-gray-400">
          This job has been {currentStatus}. No further actions available.
        </p>
      )}
    </div>
  );
}
