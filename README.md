# Employee Attendance & Analytics API

A REST API for employee attendance management and analytics, built with **FastAPI**, **PyMongo**, and **MongoDB** for the HROne Software Engineer Trainee assignment.

## Tech Stack

- **Python 3.11+**
- **FastAPI** — REST endpoints and request validation
- **Uvicorn** — ASGI development server
- **PyMongo** — MongoDB driver
- **MongoDB 6.0+** — persistent document database and aggregation pipeline execution

## Prerequisites

Install Python 3.11 or newer and MongoDB 6.0 or newer. MongoDB can run locally or through a MongoDB Atlas cluster.

Check the installations:

```bash
python --version
python -m pip --version
```

If using a local MongoDB installation, make sure the MongoDB server service is running before starting the API.

## Setup

From the repository root:

```bash
python -m pip install -r requirements.txt
```

Configure the database connection using environment variables:

- `MONGO_URI` — MongoDB connection string
- `MONGO_DB` — database name (for example, `attendance_db`)

### Windows PowerShell example

```powershell
$env:MONGO_URI = "mongodb://localhost:27017/"
$env:MONGO_DB = "attendance_db"
python -m uvicorn app.main:app --reload --port 8000
```

### macOS/Linux example

```bash
export MONGO_URI="mongodb://localhost:27017/"
export MONGO_DB="attendance_db"
python -m uvicorn app.main:app --reload --port 8000
```

For MongoDB Atlas, set `MONGO_URI` to your Atlas connection string instead. Do not commit credentials, connection strings containing passwords, or `.env` files to GitHub. Environment variables should take precedence over any local development configuration.

## Run the API

```bash
python -m uvicorn app.main:app --reload --port 8000
```

When the server starts, open:

- Health check: http://127.0.0.1:8000/health
- Interactive Swagger documentation: http://127.0.0.1:8000/docs

The health endpoint should return HTTP `200` when the application is healthy.

## MongoDB and Indexes

The API stores employee and attendance records in MongoDB. MongoDB aggregation pipelines are used for analytics, and indexes support common filters, sorting, lookups, and uniqueness constraints.

The application should create its required indexes idempotently at startup. In particular, the employee code and employee/date attendance key must preserve the uniqueness rules required by the API. Index creation should be verified against the running application and MongoDB instance.

To inspect indexes using the MongoDB shell (`mongosh`), connect to the same database configured by `MONGO_URI` and `MONGO_DB`, then run:

```javascript
use attendance_db
db.employees.getIndexes()
db.attendance.getIndexes()
```

If your configured database name is different, replace `attendance_db` with that name. Collection names must match those used by the application.

## Explain / Query Plan Endpoints

The following examples expose MongoDB query-plan information. Use parameters and records that exist in your database:

- `/admin/explain/attendance_list`
- `/admin/explain/employee_monthly?emp_code=EMP1001&month=2026-10`
- `/admin/explain/department_summary?month=2026-10`
- `/admin/explain/late_leaderboard?date_from=2026-10-01&date_to=2026-10-31&limit=10`
- `/admin/explain/department_trend?department=Engineering&from=2026-10-01&to=2026-10-10`

Inspect `executionStats` in the response. An explain response by itself does not prove that the query is efficient; verify the winning plan, examined documents/keys, and whether an unexpected collection scan occurs. Performance claims should be based on tests with a representative dataset, not only a small development database.

## Before Submitting

- Verify every finding in `REVIEW.md` against the original starter and final implementation; remove unconfirmed findings.
- Ensure `DECISIONS.md` meets the assignment's 150–200 word limit.
- Test CRUD behavior, validation, concurrency, analytics edge cases, and performance with approximately 100,000 attendance records.
- Commit the required source and documentation files only.
- Never commit `.env`, credentials, virtual environments, database dumps, or generated benchmark data.

## Verification Status

Document only checks that were actually run. Do not claim that concurrency tests, the full edge-case suite, or 100,000-record performance requirements passed unless those tests were executed and their results recorded.
