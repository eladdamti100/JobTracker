"""JobTracker REST API — serves data to the Next.js dashboard."""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import func

from db.database import init_db, get_session
from db.models import SuggestedJob, Application

load_dotenv()

app = Flask(__name__)
CORS(app, origins="*")

init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _suggested_to_dict(job: SuggestedJob) -> dict:
    return {
        "id": job.id,
        "job_hash": job.job_hash,
        "company": job.company,
        "title": job.title,
        "source": job.source,
        "apply_url": job.apply_url,
        "location": job.location,
        "description": job.description,
        "date_posted": job.date_posted,
        "salary": job.salary,
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
        # Timestamps
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "responded_at": job.responded_at.isoformat() if job.responded_at else None,
    }


def _application_to_dict(app_record: Application) -> dict:
    return {
        "id": app_record.id,
        "job_hash": app_record.job_hash,
        "company": app_record.company,
        "title": app_record.title,
        "source": app_record.source,
        "apply_url": app_record.apply_url,
        # Application details
        "applied_at": app_record.applied_at.isoformat() if app_record.applied_at else None,
        "application_method": app_record.application_method,
        "application_result": app_record.application_result,
        "status": app_record.status,
        # Evidence
        "screenshot_path": app_record.screenshot_path,
        "cover_letter_used": app_record.cover_letter_used,
        "error_message": app_record.error_message,
    }


# ---------------------------------------------------------------------------
# Routes — Suggested Jobs
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/suggested")
def list_suggested():
    session = get_session()
    try:
        query = session.query(SuggestedJob)

        status = request.args.get("status")
        level = request.args.get("level")
        source = request.args.get("source")
        search = request.args.get("search", "").strip()
        sort = request.args.get("sort", "created_at")
        order = request.args.get("order", "desc")
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))

        if status:
            query = query.filter(SuggestedJob.status == status)
        if level:
            query = query.filter(SuggestedJob.level == level)
        if source:
            query = query.filter(SuggestedJob.source == source)
        if search:
            like = f"%{search}%"
            query = query.filter(
                SuggestedJob.title.ilike(like) | SuggestedJob.company.ilike(like)
            )

        sort_col = {
            "score": SuggestedJob.score,
            "company": SuggestedJob.company,
            "status": SuggestedJob.status,
            "expires_at": SuggestedJob.expires_at,
        }.get(sort, SuggestedJob.created_at)

        query = query.order_by(sort_col.asc() if order == "asc" else sort_col.desc())

        total = query.count()
        jobs = query.offset((page - 1) * per_page).limit(per_page).all()

        return jsonify({
            "jobs": [_suggested_to_dict(j) for j in jobs],
            "total": total,
            "page": page,
            "per_page": per_page,
        })
    finally:
        session.close()


@app.route("/api/suggested/<job_hash>")
def get_suggested(job_hash):
    session = get_session()
    try:
        job = session.query(SuggestedJob).filter(SuggestedJob.job_hash == job_hash).first()
        if not job:
            return jsonify({"error": "Suggested job not found"}), 404
        return jsonify(_suggested_to_dict(job))
    finally:
        session.close()


@app.route("/api/suggested/<job_hash>", methods=["PATCH"])
def update_suggested(job_hash):
    """Approve or reject a suggested job from the dashboard."""
    session = get_session()
    try:
        job = session.query(SuggestedJob).filter(SuggestedJob.job_hash == job_hash).first()
        if not job:
            return jsonify({"error": "Suggested job not found"}), 404

        data = request.get_json() or {}
        allowed = ["status"]
        for field in allowed:
            if field in data:
                setattr(job, field, data[field])

        if "status" in data and data["status"] in ("approved", "rejected"):
            job.responded_at = datetime.now(timezone.utc)

        session.commit()
        return jsonify(_suggested_to_dict(job))
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Routes — Applications
# ---------------------------------------------------------------------------

@app.route("/api/applications")
def list_applications():
    session = get_session()
    try:
        query = session.query(Application)

        status = request.args.get("status")
        source = request.args.get("source")
        search = request.args.get("search", "").strip()
        sort = request.args.get("sort", "applied_at")
        order = request.args.get("order", "desc")
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))

        if status:
            query = query.filter(Application.status == status)
        if source:
            query = query.filter(Application.source == source)
        if search:
            like = f"%{search}%"
            query = query.filter(
                Application.title.ilike(like) | Application.company.ilike(like)
            )

        sort_col = {
            "company": Application.company,
            "status": Application.status,
        }.get(sort, Application.applied_at)

        query = query.order_by(sort_col.asc() if order == "asc" else sort_col.desc())

        total = query.count()
        apps = query.offset((page - 1) * per_page).limit(per_page).all()

        return jsonify({
            "applications": [_application_to_dict(a) for a in apps],
            "total": total,
            "page": page,
            "per_page": per_page,
        })
    finally:
        session.close()


@app.route("/api/applications/<job_hash>")
def get_application(job_hash):
    session = get_session()
    try:
        app_record = session.query(Application).filter(Application.job_hash == job_hash).first()
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
        return jsonify(_application_to_dict(app_record))
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Routes — Stats
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def get_stats():
    session = get_session()
    try:
        # Suggested stats
        suggested_total = session.query(SuggestedJob).count()
        suggested_by_status = dict(
            session.query(SuggestedJob.status, func.count(SuggestedJob.id))
            .group_by(SuggestedJob.status).all()
        )
        suggested_by_level = dict(
            session.query(SuggestedJob.level, func.count(SuggestedJob.id))
            .filter(SuggestedJob.level.isnot(None))
            .group_by(SuggestedJob.level).all()
        )
        suggested_by_source = dict(
            session.query(SuggestedJob.source, func.count(SuggestedJob.id))
            .filter(SuggestedJob.source.isnot(None))
            .group_by(SuggestedJob.source).all()
        )

        # Application stats
        app_total = session.query(Application).count()
        app_by_status = dict(
            session.query(Application.status, func.count(Application.id))
            .group_by(Application.status).all()
        )

        # Recent applications
        recent_apps = (
            session.query(Application)
            .order_by(Application.applied_at.desc())
            .limit(10)
            .all()
        )

        # Pending suggestions
        pending_suggestions = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status == "suggested")
            .order_by(SuggestedJob.created_at.desc())
            .limit(5)
            .all()
        )

        return jsonify({
            "suggested": {
                "total": suggested_total,
                "by_status": suggested_by_status,
                "by_level": suggested_by_level,
                "by_source": suggested_by_source,
                "pending": [_suggested_to_dict(j) for j in pending_suggestions],
            },
            "applications": {
                "total": app_total,
                "by_status": app_by_status,
                "recent": [_application_to_dict(a) for a in recent_apps],
            },
        })
    finally:
        session.close()


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 5001))
    print(f"JobTracker API running on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
