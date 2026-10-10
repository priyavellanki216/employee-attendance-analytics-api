

"""Employee Attendance & Analytics API."""

import os

import re

from datetime import date, datetime, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query

from pydantic import BaseModel, Field, field_validator, model_validator

from pymongo import MongoClient, ReturnDocument

from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(

    os.getenv("MONGO_URI", "mongodb://localhost:27017"),

    serverSelectionTimeoutMS=3000,

)

db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")

STATUSES = (*PRESENCE, "ABSENT", "LEAVE")

MIN_TS, MAX_TS = 100_000_000_000, 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")

def create_indexes():

    db.employees.create_index("emp_code", unique=True, name="uq_employee_code")

    db.employees.create_index(

        [("department", 1), ("emp_code", 1)],

        name="ix_employee_department_code",

    )

    db.employees.create_index(

        [("department", 1), ("joined_on", 1)],

        name="ix_employee_department_joined",

    )

    db.attendance_logs.create_index(

        [("emp_code", 1), ("date", 1)],

        unique=True,

        name="uq_attendance_employee_date",

    )

    db.attendance_logs.create_index(

        [("date", -1), ("emp_code", 1)],

        name="ix_attendance_date_employee",

    )

    db.attendance_logs.create_index(

        [("emp_code", 1), ("date", -1)],

        name="ix_attendance_employee_date",

    )

    db.attendance_logs.create_index(

        [("date", 1), ("late_minutes", -1)],

        name="ix_attendance_date_late",

    )

def fail(status: int, detail: str):

    raise HTTPException(status_code=status, detail=detail)

def parse_date(value: str) -> date:

    try:

        result = datetime.strptime(value, "%Y-%m-%d").date()

        if result.isoformat() != value:

            raise ValueError

        return result

    except (ValueError, TypeError):

        fail(422, "Date must use YYYY-MM-DD format")

def parse_month(value: str):

    if not isinstance(value, str) or not re.fullmatch(

        r"\d{4}-(0[1-9]|1[0-2])", value

    ):

        fail(422, "month must use YYYY-MM format")

    first = date.fromisoformat(value + "-01")

    next_month = date(

        first.year + (first.month == 12),

        1 if first.month == 12 else first.month + 1,

        1,

    )

    return first, next_month - timedelta(days=1)

def instant(value: Optional[int]) -> datetime:

    if value is None:

        return datetime.now(UTC).replace(microsecond=0)

    if type(value) is not int or not MIN_TS <= value <= MAX_TS:

        fail(422, "Timestamp must be integer epoch milliseconds in range")

    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)

def as_utc(value):

    if value is None:

        return None

    if value.tzinfo is None:

        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)

def to_ms(value):

    value = as_utc(value)

    return int(value.timestamp() * 1000) if value is not None else None

def as_ist(value):

    return as_utc(value).astimezone(IST)

def local_day(punch_in, employee):

    local = as_ist(punch_in)

    if (

        employee["shift_end"] <= employee["shift_start"]

        and local.strftime("%H:%M") < employee["shift_end"]

    ):

        local -= timedelta(days=1)

    return local.date().isoformat()

def shift_start_dt(day_text, employee):

    day = parse_date(day_text)

    hour, minute = map(int, employee["shift_start"].split(":"))

    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)

def shift_end_dt(day_text, employee):

    day = parse_date(day_text)

    hour, minute = map(int, employee["shift_end"].split(":"))

    if employee["shift_end"] <= employee["shift_start"]:

        day += timedelta(days=1)

    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)

def calc_late(punch_in, day_text, employee):

    start = shift_start_dt(day_text, employee)

    elapsed = int((as_ist(punch_in) - start).total_seconds())

    return elapsed // 60 if elapsed > 600 else 0

def calc_overtime(punch_out, day_text, employee):

    end = shift_end_dt(day_text, employee)

    elapsed = int((as_ist(punch_out) - end).total_seconds())

    minutes = elapsed // 60 if elapsed > 0 else 0

    return minutes if minutes >= 30 else 0

def calc_hours(punch_in, punch_out):

    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())

    result = (

        Decimal(seconds) / Decimal("3600")

    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return float(result)

def round4(value):

    return float(

        Decimal(str(value)).quantize(

            Decimal("0.0001"), rounding=ROUND_HALF_UP

        )

    )

def weekdays(start: date, end: date):

    count = 0

    while start <= end:

        if start.weekday() < 5:

            count += 1

        start += timedelta(days=1)

    return count

def serialize_employee(doc):

    doc.pop("_id", None)

    if "created_at" in doc:

        doc["created_at"] = to_ms(doc["created_at"])

    return doc

def serialize_attendance(doc):

    doc.pop("_id", None)

    doc["punch_in"] = to_ms(doc.get("punch_in"))

    doc["punch_out"] = to_ms(doc.get("punch_out"))

    history = []

    for entry in doc.get("history") or []:

        item = {

            "at": to_ms(entry.get("at")),

            "by": entry.get("by", ""),

            "reason": entry.get("reason", ""),

            "changes": {},

        }

        for field, change in (entry.get("changes") or {}).items():

            old = change.get("from")

            new = change.get("to")

            if field in ("punch_in", "punch_out"):

                old, new = to_ms(old), to_ms(new)

            item["changes"][field] = {"from": old, "to": new}

        history.append(item)

    doc["history"] = history

    doc.setdefault("late_minutes", 0)

    doc.setdefault("overtime_minutes", 0)

    doc.setdefault("half_day", False)

    return doc

class EmployeeIn(BaseModel):

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(

        min_length=1,

        max_length=120,

        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",

    )

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = "09:30"

    shift_end: str = "18:30"

    joined_on: str

    @field_validator("shift_start", "shift_end")

    @classmethod

    def validate_shift_time(cls, value):

        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):

            raise ValueError("Time must use HH:MM format")

        return value

    @field_validator("joined_on")

    @classmethod

    def validate_joined_on(cls, value):

        parse_date(value)

        return value

    @model_validator(mode="after")

    def validate_shift(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self

class PunchInRequest(BaseModel):

    emp_code: str

    punched_at: Optional[int] = None

    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")

    @classmethod

    def validate_timestamp(cls, value):

        if value is not None and (

            type(value) is not int or not MIN_TS <= value <= MAX_TS

        ):

            raise ValueError("punched_at must be integer epoch milliseconds")

        return value

    @field_validator("status")

    @classmethod

    def validate_status(cls, value):

        if value not in PRESENCE:

            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")

        return value

class PunchOutRequest(BaseModel):

    emp_code: str

    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")

    @classmethod

    def validate_timestamp(cls, value):

        if value is not None and (

            type(value) is not int or not MIN_TS <= value <= MAX_TS

        ):

            raise ValueError("punched_at must be integer epoch milliseconds")

        return value

class RegularizeRequest(BaseModel):

    status: Optional[str] = None

    punch_in: Optional[int] = None

    punch_out: Optional[int] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")

    @classmethod

    def validate_status(cls, value):

        if value is not None and value not in STATUSES:

            raise ValueError("Invalid attendance status")

        return value

    @field_validator("punch_in", "punch_out", mode="before")

    @classmethod

    def validate_timestamps(cls, value):

        if value is not None and (

            type(value) is not int or not MIN_TS <= value <= MAX_TS

        ):

            raise ValueError("Punch times must be integer epoch milliseconds")

        return value

@app.get("/health")

def health():

    try:

        db.command("ping")

        return {"status": "ok"}

    except Exception:

        fail(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:

        db.employees.insert_one(doc)

    except DuplicateKeyError:

        fail(409, "emp_code already exists")

    return serialize_employee(doc)

@app.get("/employees")

def list_employees(

    department: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    query = {"department": department} if department is not None else {}

    total = db.employees.count_documents(query)

    cursor = (

        db.employees.find(query, {"_id": 0})

        .sort("emp_code", 1)

        .skip((page - 1) * page_size)

        .limit(page_size)

    )

    return {

        "items": [serialize_employee(item) for item in cursor],

        "total": total,

        "page": page,

        "page_size": page_size,

    }

@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInRequest):

    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:

        fail(404, "Employee not found")

    stamp = instant(body.punched_at)

    day_text = local_day(stamp, employee)

    record = {

        "emp_code": body.emp_code,

        "date": day_text,

        "status": body.status,

        "punch_in": stamp,

        "punch_out": None,

        "work_hours": None,

        "late_minutes": calc_late(stamp, day_text, employee),

        "overtime_minutes": 0,

        "half_day": False,

        "history": [],

    }

    try:

        db.attendance_logs.insert_one(record)

    except DuplicateKeyError:

        fail(409, "Already punched in for this attendance date")

    return serialize_attendance(record)

@app.post("/attendance/punch-out")

def punch_out(body: PunchOutRequest):

    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:

        fail(404, "Employee not found")

    stamp = instant(body.punched_at)

    record = db.attendance_logs.find_one(

        {

            "emp_code": body.emp_code,

            "punch_in": {"$lte": stamp},

        },

        sort=[("punch_in", -1)],

    )

    if not record:

        fail(404, "No eligible punch-in record found")

    if record.get("punch_out") is not None:

        fail(409, "Attendance record is already punched out")

    if (

        stamp <= as_utc(record["punch_in"])

        or stamp > as_utc(record["punch_in"]) + timedelta(hours=24)

    ):

        fail(422, "punch_out must be after punch_in and within 24 hours")

    hours = calc_hours(record["punch_in"], stamp)

    updated = db.attendance_logs.find_one_and_update(

        {"_id": record["_id"], "punch_out": None},

        {

            "$set": {

                "punch_out": stamp,

                "work_hours": hours,

                "overtime_minutes": calc_overtime(

                    stamp, record["date"], employee

                ),

                "half_day": hours < 4.50,

            }

        },

        return_document=ReturnDocument.AFTER,

    )

    if updated is None:

        fail(409, "Attendance record was punched out concurrently")

    return serialize_attendance(updated)

@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    query = {}

    if emp_code is not None:

        query["emp_code"] = emp_code

    if date_from:

        parse_date(date_from)

        query.setdefault("date", {})["$gte"] = date_from

    if date_to:

        parse_date(date_to)

        query.setdefault("date", {})["$lte"] = date_to

    if date_from and date_to and date_from > date_to:

        fail(422, "date_from must not be after date_to")

    if status is not None:

        if status not in STATUSES:

            fail(422, "Invalid status")

        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    cursor = (

        db.attendance_logs.find(query, {"_id": 0})

        .sort([("date", -1), ("emp_code", 1)])

        .skip((page - 1) * page_size)

        .limit(page_size)

    )

    return {

        "items": [serialize_attendance(item) for item in cursor],

        "total": total,

        "page": page,

        "page_size": page_size,

    }

@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):

    parse_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:

        fail(404, "Employee not found")

    current = db.attendance_logs.find_one(

        {"emp_code": emp_code, "date": date}

    )

    if not current:

        fail(404, "Attendance record not found")

    supplied = body.model_fields_set - {"reason", "regularized_by"}

    if not supplied:

        fail(422, "At least one attendance field must be supplied")

    status = body.status if "status" in supplied else current["status"]

    pin = (

        instant(body.punch_in)

        if "punch_in" in supplied and body.punch_in is not None

        else current.get("punch_in")

    )

    pout = (

        instant(body.punch_out)

        if "punch_out" in supplied and body.punch_out is not None

        else current.get("punch_out")

    )

    if status in ("ABSENT", "LEAVE"):

        if (

            ("punch_in" in supplied and body.punch_in is not None)

            or ("punch_out" in supplied and body.punch_out is not None)

        ):

            fail(422, "ABSENT and LEAVE cannot include punch times")

        pin = pout = None

    elif pin is None:

        fail(422, "Presence status requires punch_in")

    if pin is not None and local_day(pin, employee) != date:

        fail(422, "punch_in must belong to the attendance date")

    if pout is not None and (

        pin is None or pout <= pin or pout > pin + timedelta(hours=24)

    ):

        fail(422, "punch_out must be after punch_in and within 24 hours")

    derived = {

        "work_hours": (

            calc_hours(pin, pout)

            if pin is not None and pout is not None

            else None

        ),

        "late_minutes": (

            calc_late(pin, date, employee)

            if pin is not None and status in PRESENCE

            else 0

        ),

        "overtime_minutes": (

            calc_overtime(pout, date, employee)

            if pout is not None and status in PRESENCE

            else 0

        ),

        "half_day": bool(

            pin is not None

            and pout is not None

            and status in PRESENCE

            and calc_hours(pin, pout) < 4.50

        ),

    }

    proposed = {

        "status": status,

        "punch_in": pin,

        "punch_out": pout,

        **derived,

    }

    changes = {}

    keys = (

        "status", "punch_in", "punch_out", "work_hours",

        "late_minutes", "overtime_minutes", "half_day",

    )

    for key in keys:

        old = current.get(

            key,

            False if key == "half_day"

            else 0 if key in ("late_minutes", "overtime_minutes")

            else None,

        )

        new = proposed[key]

        if old != new:

            changes[key] = {"from": old, "to": new}

    if not changes:

        fail(422, "Request does not change any values")

    history_entry = {

        "at": datetime.now(UTC).replace(microsecond=0),

        "by": body.regularized_by,

        "reason": body.reason,

        "changes": changes,

    }

    updated = db.attendance_logs.find_one_and_update(

        {

            "_id": current["_id"],

            "history": current.get("history", []),

        },

        {

            "$set": proposed,

            "$push": {"history": history_entry},

        },

        return_document=ReturnDocument.AFTER,

    )

    if updated is None:

        fail(409, "Attendance record changed concurrently; retry")

    return serialize_attendance(updated)

def month_match(start, end):

    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str):

    month_start, month_end = parse_month(month)

    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:

        fail(404, "Employee not found")

    joined = parse_date(employee["joined_on"])

    working_start = max(month_start, joined)

    working_days = (

        weekdays(working_start, month_end)

        if working_start <= month_end

        else 0

    )

    pipeline = [

        {

            "$match": {

                "emp_code": emp_code,

                **month_match(month_start, month_end),

            }

        },

        {

            "$set": {

                "_day": {

                    "$dayOfWeek": {

                        "$dateFromString": {"dateString": "$date"}

                    }

                }

            }

        },

        {

            "$group": {

                "_id": None,

                "present_days": {

                    "$sum": {

                        "$cond": [

                            {

                                "$and": [

                                    {"$in": ["$status", list(PRESENCE)]},

                                    {"$gt": ["$_day", 1]},

                                    {"$lt": ["$_day", 7]},

                                ]

                            },

                            {"$cond": ["$half_day", 0.5, 1]},

                            0,

                        ]

                    }

                },

                "leave_days": {

                    "$sum": {

                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]

                    }

                },

                "late_count": {

                    "$sum": {

                        "$cond": [

                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},

                            1, 0,

                        ]

                    }

                },

                "total_late_minutes": {

                    "$sum": {"$ifNull": ["$late_minutes", 0]}

                },

                "total_overtime_minutes": {

                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}

                },

            }

        },

    ]

    row = next(db.attendance_logs.aggregate(pipeline), {})

    present = row.get("present_days", 0)

    return {

        "emp_code": emp_code,

        "month": month,

        "working_days": working_days,

        "present_days": present,

        "leave_days": row.get("leave_days", 0),

        "late_count": row.get("late_count", 0),

        "total_late_minutes": row.get("total_late_minutes", 0),

        "total_overtime_minutes": row.get("total_overtime_minutes", 0),

        "attendance_pct": round4(present / working_days * 100)

        if working_days else None,

    }

@app.get("/analytics/departments/summary")

def department_summary(month: str, department: Optional[str] = None):

    start, end = parse_month(month)

    employee_match = {"joined_on": {"$lte": end.isoformat()}}

    if department is not None:

        employee_match["department"] = department

    pipeline = [

        {"$match": employee_match},

        {

            "$lookup": {

                "from": "attendance_logs",

                "let": {"code": "$emp_code"},

                "pipeline": [

                    {

                        "$match": {

                            "$expr": {

                                "$and": [

                                    {"$eq": ["$emp_code", "$$code"]},

                                    {"$gte": ["$date", start.isoformat()]},

                                    {"$lte": ["$date", end.isoformat()]},

                                ]

                            }

                        }

                    },

                    {

                        "$project": {

                            "date": 1, "status": 1, "work_hours": 1,

                            "late_minutes": 1, "half_day": 1,

                            "overtime_minutes": 1,

                        }

                    },

                ],

                "as": "logs",

            }

        },

        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        {

            "$set": {

                "_weekday": {

                    "$cond": [

                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},

                        {"$dayOfWeek": {

                            "$dateFromString": {"dateString": "$logs.date"}

                        }},

                        0,

                    ]

                }

            }

        },

        {

            "$group": {

                "_id": {

                    "department": "$department",

                    "emp_code": "$emp_code",

                },

                "present_days": {

                    "$sum": {

                        "$cond": [

                            {

                                "$and": [

                                    {"$in": ["$logs.status", list(PRESENCE)]},

                                    {"$gt": ["$_weekday", 1]},

                                    {"$lt": ["$_weekday", 7]},

                                ]

                            },

                            {"$cond": ["$logs.half_day", 0.5, 1]},

                            0,

                        ]

                    }

                },

                "hours": {

                    "$push": {

                        "$cond": [

                            {

                                "$and": [

                                    {"$in": ["$logs.status", list(PRESENCE)]},

                                    {"$ne": ["$logs.work_hours", None]},

                                ]

                            },

                            "$logs.work_hours",

                            None,

                        ]

                    }

                },

                "late_count": {

                    "$sum": {

                        "$cond": [

                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},

                            1, 0,

                        ]

                    }

                },

                "total_late_minutes": {

                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}

                },

                "leave_count": {

                    "$sum": {

                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]

                    }

                },

                "on_duty_count": {

                    "$sum": {

                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]

                    }

                },

            }

        },

        {

            "$group": {

                "_id": "$_id.department",

                "headcount": {"$sum": 1},

                "present_days": {"$sum": "$present_days"},

                "hours": {"$push": "$hours"},

                "late_count": {"$sum": "$late_count"},

                "total_late_minutes": {"$sum": "$total_late_minutes"},

                "leave_count": {"$sum": "$leave_count"},

                "on_duty_count": {"$sum": "$on_duty_count"},

            }

        },

        {

            "$project": {

                "_id": 0,

                "department": "$_id",

                "headcount": 1,

                "present_days": 1,

                "late_count": 1,

                "total_late_minutes": 1,

                "leave_count": 1,

                "on_duty_count": 1,

                "all_hours": {

                    "$reduce": {

                        "input": "$hours",

                        "initialValue": [],

                        "in": {"$concatArrays": ["$$value", "$$this"]},

                    }

                },

            }

        },

        {

            "$project": {

                "department": 1, "headcount": 1, "present_days": 1,

                "late_count": 1, "total_late_minutes": 1,

                "leave_count": 1, "on_duty_count": 1,

                "valid_hours": {

                    "$filter": {

                        "input": "$all_hours",

                        "as": "h",

                        "cond": {"$ne": ["$$h", None]},

                    }

                },

            }

        },

        {

            "$project": {

                "department": 1, "headcount": 1, "present_days": 1,

                "late_count": 1, "total_late_minutes": 1,

                "leave_count": 1, "on_duty_count": 1,

                "avg_work_hours": {

                    "$cond": [

                        {"$gt": [{"$size": "$valid_hours"}, 0]},

                        {"$round": [{"$avg": "$valid_hours"}, 2]},

                        None,

                    ]

                },

            }

        },

        {"$sort": {"department": 1}},

    ]

    return {"month": month, "items": list(db.employees.aggregate(pipeline))}

@app.get("/analytics/leaderboard/late")

def late_leaderboard(

    month: str,

    limit: int = Query(10, ge=1, le=50),

    department: Optional[str] = None,

):

    start, end = parse_month(month)

    pipeline = [

        {

            "$match": {

                **month_match(start, end),

                "late_minutes": {"$gt": 0},

            }

        },

        {

            "$group": {

                "_id": "$emp_code",

                "total_late_minutes": {"$sum": "$late_minutes"},

                "late_count": {"$sum": 1},

            }

        },

        {

            "$lookup": {

                "from": "employees",

                "localField": "_id",

                "foreignField": "emp_code",

                "as": "employee",

            }

        },

        {"$unwind": "$employee"},

    ]

    if department is not None:

        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([

        {

            "$setWindowFields": {

                "sortBy": {"total_late_minutes": -1},

                "output": {"rank": {"$rank": {}}},

            }

        },

        {"$match": {"rank": {"$lte": limit}}},

        {

            "$project": {

                "_id": 0,

                "emp_code": "$_id",

                "name": "$employee.name",

                "department": "$employee.department",

                "total_late_minutes": 1,

                "late_count": 1,

                "rank": 1,

            }

        },

        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},

    ])

    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")

def department_trend(

    department: str,

    from_date: str = Query(alias="from"),

    to_date: str = Query(alias="to"),

):

    start, end = parse_date(from_date), parse_date(to_date)

    if end < start:

        fail(422, "to must not be before from")

    if (end - start).days + 1 > 92:

        fail(422, "Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):

        fail(404, "Department not found")

    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)

    end_dt = datetime(end.year, end.month, end.day, tzinfo=UTC)

    pipeline = [

        {

            "$match": {

                "department": department,

                "joined_on": {"$lte": end.isoformat()},

            }

        },

        {

            "$lookup": {

                "from": "attendance_logs",

                "let": {"code": "$emp_code"},

                "pipeline": [

                    {

                        "$match": {

                            "$expr": {

                                "$and": [

                                    {"$eq": ["$emp_code", "$$code"]},

                                    {"$gte": ["$date", start.isoformat()]},

                                    {"$lte": ["$date", end.isoformat()]},

                                ]

                            }

                        }

                    },

                    {

                        "$project": {

                            "date": 1, "status": 1, "half_day": 1,

                            "late_minutes": 1,

                        }

                    },

                ],

                "as": "logs",

            }

        },

        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        {

            "$group": {

                "_id": {"day": "$logs.date", "emp": "$emp_code"},

                "status": {"$first": "$logs.status"},

                "half_day": {"$first": "$logs.half_day"},

                "late_minutes": {"$first": "$logs.late_minutes"},

            }

        },

        {

            "$group": {

                "_id": "$_id.day",

                "present_count": {

                    "$sum": {

                        "$cond": [

                            {"$in": ["$status", list(PRESENCE)]},

                            {"$cond": ["$half_day", 0.5, 1]},

                            0,

                        ]

                    }

                },

                "late_count": {

                    "$sum": {

                        "$cond": [

                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},

                            1, 0,

                        ]

                    }

                },

            }

        },

        {

            "$project": {

                "_id": 0,

                "day": {

                    "$dateFromString": {

                        "dateString": {"$ifNull": ["$_id", start.isoformat()]}

                    }

                },

                "present_count": 1,

                "late_count": 1,

            }

        },

        {

            "$densify": {

                "field": "day",

                "range": {

                    "step": 1,

                    "unit": "day",

                    "bounds": [start_dt, end_dt + timedelta(days=1)],

                },

            }

        },

        {"$match": {"day": {"$gte": start_dt, "$lte": end_dt}}},

        {

            "$set": {

                "date": {

                    "$dateToString": {"format": "%Y-%m-%d", "date": "$day"}

                }

            }

        },

        {"$sort": {"day": 1}},

    ]

    # Complete all daily calculations in MongoDB. The lookup counts employees
    # who have joined by each day; $setWindowFields calculates the rolling
    # seven-row average while MongoDB's $avg ignores null weekend rates.
    pipeline.extend([
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_text": {"$dateToString": {
                    "format": "%Y-%m-%d", "date": "$day"
                }}},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$department", department]},
                        {"$lte": ["$joined_on", "$$day_text"]},
                    ]}}},
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]},
                "is_working_day": {"$and": [
                    {"$gt": [{"$dayOfWeek": "$day"}, 1]},
                    {"$lt": [{"$dayOfWeek": "$day"}, 7]},
                ]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        "$is_working_day",
                        {"$cond": [
                            {"$gt": ["$headcount", 0]},
                            {"$round": [{"$multiply": [
                                {"$divide": ["$present_count", "$headcount"]}, 100
                            ]}, 4]},
                            0,
                        ]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                "present_count": 1, "late_count": 1, "attendance_rate": 1,
                "moving_avg_7d": {"$cond": [
                    {"$eq": ["$moving_avg_7d", None]},
                    None, {"$round": ["$moving_avg_7d", 4]}
                ]},
            }
        },
        {"$sort": {"day": 1}},
    ])
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}
@app.get("/admin/explain/{endpoint}")

def explain_endpoint(

    endpoint: str,

    emp_code: Optional[str] = None,

    month: Optional[str] = None,

    department: Optional[str] = None,

    limit: int = Query(10, ge=1, le=50),

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[str] = None,

    from_date: Optional[str] = Query(None, alias="from"),

    to_date: Optional[str] = Query(None, alias="to"),

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    allowed = {

        "attendance_list", "employee_monthly", "department_summary",

        "late_leaderboard", "department_trend",

    }

    if endpoint not in allowed:

        fail(422, "Unknown endpoint")

    if endpoint == "attendance_list":

        query = {}

        if emp_code:

            query["emp_code"] = emp_code

        if date_from:

            parse_date(date_from)

            query.setdefault("date", {})["$gte"] = date_from

        if date_to:

            parse_date(date_to)

            query.setdefault("date", {})["$lte"] = date_to

        if date_from and date_to and date_from > date_to:

            fail(422, "date_from must not be after date_to")

        if status:

            if status not in STATUSES:

                fail(422, "Invalid status")

            query["status"] = status

        operation = {

            "find": "attendance_logs",

            "filter": query,

            "sort": {"date": -1, "emp_code": 1},

            "skip": (page - 1) * page_size,

            "limit": page_size,

        }

        collection = "attendance_logs"

    elif endpoint == "employee_monthly":

        if not emp_code or not month:

            fail(422, "emp_code and month are required")

        start, end = parse_month(month)

        query = {"emp_code": emp_code, **month_match(start, end)}

        operation = {

            "aggregate": "attendance_logs",

            "pipeline": [

                {"$match": query},

                {

                    "$group": {

                        "_id": "$emp_code",

                        "late": {"$sum": "$late_minutes"},

                    }

                },

            ],

            "cursor": {},

        }

        collection = "attendance_logs"

    elif endpoint == "department_summary":

        if not month:

            fail(422, "month is required")

        _, end = parse_month(month)

        query = {"joined_on": {"$lte": end.isoformat()}}

        if department is not None:

            query["department"] = department

        operation = {

            "aggregate": "employees",

            "pipeline": [

                {"$match": query},

                {

                    "$lookup": {

                        "from": "attendance_logs",

                        "localField": "emp_code",

                        "foreignField": "emp_code",

                        "as": "logs",

                    }

                },

            ],

            "cursor": {},

        }

        collection = "employees"

    elif endpoint == "late_leaderboard":

        if not month:

            fail(422, "month is required")

        start, end = parse_month(month)

        query = {

            **month_match(start, end),

            "late_minutes": {"$gt": 0},

        }

        pipeline = [

            {"$match": query},

            {

                "$group": {

                    "_id": "$emp_code",

                    "total_late_minutes": {"$sum": "$late_minutes"},

                }

            },

        ]

        if department is not None:

            pipeline.extend([

                {

                    "$lookup": {

                        "from": "employees",

                        "localField": "_id",

                        "foreignField": "emp_code",

                        "as": "employee",

                    }

                },

                {"$unwind": "$employee"},

                {"$match": {"employee.department": department}},

            ])

        operation = {

            "aggregate": "attendance_logs",

            "pipeline": pipeline,

            "cursor": {},

        }

        collection = "attendance_logs"

    else:

        if not department or not from_date or not to_date:

            fail(422, "department, from and to are required")

        start, end = parse_date(from_date), parse_date(to_date)

        if end < start or (end - start).days + 1 > 92:

            fail(422, "Invalid trend date range")

        query = {

            "date": {

                "$gte": start.isoformat(),

                "$lte": end.isoformat(),

            }

        }

        operation = {

            "find": "attendance_logs",

            "filter": query,

            "sort": {"date": 1},

        }

        collection = "attendance_logs"

    explain_result = db.command(

        "explain",

        operation,

        verbosity="executionStats",

    )

    return {

        "endpoint": endpoint,

        "collection": collection,

        "explain": explain_result,

    }
