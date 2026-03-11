"""JobTracker REST API — serves data to the Next.js dashboard."""

import sys
import io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import func

from db.database import init_db, get_session
from db.models import Job

load_dotenv()

app = Flask(__name__)
CORS(app, origins="*")

init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_source(job_id: str) -> str:
    if job_id.startswith("HMT-"):
        return "HireMeTech"
    if job_id.startswith("LI-"):
        return "LinkedIn"
    if job_id.startswith("WA-"):
        return "WhatsApp"
    return "Unknown"


def _job_to_dict(job: Job) -> dict:
    source = job.source or _infer_source(job.job_id)
    return {
        "id": job.id,
        "job_id": job.job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "description": job.description,
        "apply_url": job.apply_url,
        "date_posted": job.date_posted,
        "salary": job.salary,
        "source": source,
        # Scoring
        "score": job.score,
        "reason": job.reason,
        "level": job.level,
        "role_type": job.role_type,
        "tech_stack_match": job.tech_stack_match or [],
        "is_student_position": bool(job.is_student_position),
        "apply_strategy": job.apply_strategy,
        "role_summary": job.role_summary,
        "requirements_summary": job.requirements_summary,
        # Lifecycle
        "status": job.status,
        "cover_letter_used": job.cover_letter_used,
        "error_message": job.error_message,
        # Dashboard fields
        "notes": job.notes,
        "referral_type": job.referral_type,
        "referral_url": job.referral_url,
        # Timestamps
        "found_at": job.found_at.isoformat() if job.found_at else None,
        "notified_at": job.notified_at.isoformat() if job.notified_at else None,
        "applied_at": job.applied_at.isoformat() if job.applied_at else None,
        "status_updated_at": job.status_updated_at.isoformat() if job.status_updated_at else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/jobs")
def list_jobs():
    session = get_session()
    try:
        query = session.query(Job)

        status = request.args.get("status")
        level = request.args.get("level")
        search = request.args.get("search", "").strip()
        sort = request.args.get("sort", "found_at")
        order = request.args.get("order", "desc")
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))

        if status:
            query = query.filter(Job.status == status)
        if level:
            query = query.filter(Job.level == level)
        if search:
            like = f"%{search}%"
            query = query.filter(
                Job.title.ilike(like) | Job.company.ilike(like)
            )

        sort_col = {
            "score": Job.score,
            "status": Job.status,
            "company": Job.company,
            "applied_at": Job.applied_at,
        }.get(sort, Job.found_at)

        query = query.order_by(sort_col.asc() if order == "asc" else sort_col.desc())

        total = query.count()
        jobs = query.offset((page - 1) * per_page).limit(per_page).all()

        result = [_job_to_dict(j) for j in jobs]

        # source filter is post-DB (source may be inferred)
        source = request.args.get("source")
        if source:
            result = [j for j in result if j["source"] == source]

        referral_type = request.args.get("referral_type")
        if referral_type:
            result = [j for j in result if j["referral_type"] == referral_type]

        return jsonify({"jobs": result, "total": total, "page": page, "per_page": per_page})
    finally:
        session.close()


@app.route("/api/jobs/<job_id>")
def get_job(job_id):
    session = get_session()
    try:
        job = session.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(_job_to_dict(job))
    finally:
        session.close()


@app.route("/api/jobs/<job_id>", methods=["PATCH"])
def update_job(job_id):
    session = get_session()
    try:
        job = session.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            return jsonify({"error": "Job not found"}), 404

        data = request.get_json() or {}
        allowed = ["status", "notes", "referral_type", "referral_url"]
        for field in allowed:
            if field in data:
                setattr(job, field, data[field])

        if "status" in data:
            job.status_updated_at = datetime.now(timezone.utc)
            if data["status"] == "applied" and not job.applied_at:
                job.applied_at = datetime.now(timezone.utc)

        session.commit()
        return jsonify(_job_to_dict(job))
    finally:
        session.close()


@app.route("/api/stats")
def get_stats():
    session = get_session()
    try:
        total = session.query(Job).count()

        by_status = dict(
            session.query(Job.status, func.count(Job.id))
            .group_by(Job.status)
            .all()
        )

        by_level = dict(
            session.query(Job.level, func.count(Job.id))
            .filter(Job.level.isnot(None))
            .group_by(Job.level)
            .all()
        )

        # Source breakdown (infer from job_id when source column is null)
        all_jobs = session.query(Job.job_id, Job.source).all()
        by_source: dict[str, int] = {}
        for job_id, source in all_jobs:
            s = source or _infer_source(job_id)
            by_source[s] = by_source.get(s, 0) + 1

        applied_statuses = ["applied", "in_review", "rejected", "interview", "next_stage", "accepted"]
        recent = (
            session.query(Job)
            .filter(Job.status.in_(applied_statuses))
            .order_by(Job.applied_at.desc().nullslast(), Job.found_at.desc())
            .limit(10)
            .all()
        )

        return jsonify({
            "total": total,
            "by_status": by_status,
            "by_level": by_level,
            "by_source": by_source,
            "recent": [_job_to_dict(j) for j in recent],
        })
    finally:
        session.close()


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 5001))
    print(f"JobTracker API running on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
