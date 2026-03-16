"""Reset job 929c7b17 to approved state for re-testing."""
import sqlite3

conn = sqlite3.connect('data/jobtracker.db')
c = conn.cursor()

# Check current state
c.execute("SELECT hash, status, title, company FROM suggested_jobs WHERE hash='929c7b17'")
row = c.fetchone()
print(f"Current job: {row}")

# Check applications
c.execute("SELECT id, status, created_at FROM applications WHERE job_hash='929c7b17'")
apps = c.fetchall()
print(f"Applications: {apps}")

# Check checkpoint
c.execute("SELECT job_hash, state FROM apply_checkpoints WHERE job_hash='929c7b17'")
cp = c.fetchone()
print(f"Checkpoint: {cp}")

# Reset job status to approved
c.execute("UPDATE suggested_jobs SET status='approved' WHERE hash='929c7b17'")

# Delete failed applications
c.execute("DELETE FROM applications WHERE job_hash='929c7b17'")

# Delete checkpoint
c.execute("DELETE FROM apply_checkpoints WHERE job_hash='929c7b17'")

conn.commit()
print("Reset complete. Job is now approved with no applications or checkpoint.")
conn.close()
