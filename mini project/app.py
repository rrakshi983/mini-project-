import json
import math
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("DATABASE_PATH", ROOT / "healthcare.db"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "private_uploads"))
UPLOAD_LIMIT = 5 * 1024 * 1024
PHOTO_DIR = Path(os.environ.get("PHOTO_DIR", ROOT / "doctor_photos"))
AVAILABILITY = {"Available", "Busy", "On Break", "Unavailable"}
APPOINTMENT_STATES = {"pending", "accepted", "rejected", "cancelled", "rescheduled"}
SPECIALISTS = {
    "cardiologist", "dermatologist", "neurologist", "orthopedist",
    "pediatrician", "gynecologist", "general physician",
}
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=UPLOAD_LIMIT,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK(role IN ('patient','hospital','admin')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hospitals (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER UNIQUE REFERENCES users(id),
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    phone TEXT NOT NULL DEFAULT '',
    photo_url TEXT NOT NULL DEFAULT '',
    specialties TEXT NOT NULL DEFAULT '[]',
    facilities TEXT NOT NULL DEFAULT '[]',
    emergency_services INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    is_sample INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'hospital dashboard',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doctors (
    id INTEGER PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qualification TEXT NOT NULL DEFAULT '',
    specialty TEXT NOT NULL,
    experience_years INTEGER NOT NULL DEFAULT 0,
    languages TEXT NOT NULL DEFAULT '[]',
    consultation_timings TEXT NOT NULL DEFAULT '',
    photo_url TEXT NOT NULL DEFAULT '',
    is_sample INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doctor_availability (
    doctor_id INTEGER PRIMARY KEY REFERENCES doctors(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'Unavailable',
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
    doctor_id INTEGER NOT NULL REFERENCES doctors(id),
    appointment_date TEXT NOT NULL,
    appointment_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    reminder_sent_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_appointment_slot
    ON appointments(doctor_id, appointment_date, appointment_time)
    WHERE status IN ('pending','accepted','rescheduled');
CREATE TABLE IF NOT EXISTS symptoms (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    symptoms_json TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    severity INTEGER NOT NULL,
    specialist TEXT NOT NULL,
    emergency INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS medical_history (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    condition TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    allergies TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prescriptions (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS favourites (
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(patient_id, hospital_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL DEFAULT '',
    verified_visit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hospital_verification (
    id INTEGER PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    admin_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER REFERENCES hospitals(id),
    doctor_id INTEGER REFERENCES doctors(id),
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    event TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointments(patient_id, appointment_date);
CREATE INDEX IF NOT EXISTS idx_appointments_hospital ON appointments(hospital_id, appointment_date);
CREATE INDEX IF NOT EXISTS idx_doctors_hospital ON doctors(hospital_id);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_verified_feedback_per_patient
    ON feedback(patient_id, hospital_id) WHERE verified_visit=1;
"""

DEMO_ACCOUNTS = [
    ("patient@example.com", "Patient123!", "Asha Patient", "patient"),
    ("hospital@example.com", "Hospital123!", "Demo Hospital Staff", "hospital"),
    ("admin@example.com", "Admin123!", "Platform Admin", "admin"),
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect_db():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def init_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    db = connect_db()
    try:
        db.executescript(SCHEMA)
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "phone" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
        for email, password, name, role in DEMO_ACCOUNTS:
            db.execute(
                "INSERT OR IGNORE INTO users(email,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                (email, generate_password_hash(password), name, role, now_iso()),
            )
        hospital_user = db.execute("SELECT id FROM users WHERE email=?", ("hospital@example.com",)).fetchone()
        db.execute(
            """INSERT OR IGNORE INTO hospitals(owner_id,name,address,area,city,latitude,longitude,phone,
               specialties,facilities,emergency_services,verified,is_sample,source,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                hospital_user["id"], "Academic Demo Hospital · SAMPLE ONLY",
                "Demo listing — not a real healthcare facility", "Academic sample", "Bengaluru",
                12.9716, 77.5946, "", json.dumps(["General Physician", "Dermatologist"]),
                json.dumps(["Academic sample record"]), 0, 0, 1, "academic sample seed", now_iso(),
            ),
        )
        demo_hospital = db.execute(
            "SELECT id FROM hospitals WHERE owner_id=?", (hospital_user["id"],)
        ).fetchone()
        db.execute(
            """INSERT OR IGNORE INTO doctors(hospital_id,name,qualification,specialty,experience_years,
               languages,consultation_timings,photo_url,is_sample,updated_at)
               SELECT ?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS
               (SELECT 1 FROM doctors WHERE hospital_id=? AND name=?)""",
            (
                demo_hospital["id"], "Demo Provider · SAMPLE ONLY", "Academic sample",
                "General Physician", 0, json.dumps(["English", "Kannada"]),
                "Set timings in the hospital dashboard", "", 1, now_iso(),
                demo_hospital["id"], "Demo Provider · SAMPLE ONLY",
            ),
        )
        doctor = db.execute(
            "SELECT id FROM doctors WHERE hospital_id=? AND is_sample=1 LIMIT 1",
            (demo_hospital["id"],),
        ).fetchone()
        if doctor:
            db.execute(
                "INSERT OR IGNORE INTO doctor_availability(doctor_id,status,updated_at) VALUES(?,?,?)",
                (doctor["id"], "Unavailable", now_iso()),
            )
        db.commit()
    finally:
        db.close()


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def error(message, status=400):
    return jsonify({"error": message}), status


def audit(event):
    if session.get("user_id"):
        get_db().execute(
            "INSERT INTO activity_log(user_id,event,created_at) VALUES(?,?,?)",
            (session["user_id"], event[:160], now_iso()),
        )
        get_db().commit()


def notify(user_id, message):
    get_db().execute(
        "INSERT INTO notifications(user_id,message,created_at) VALUES(?,?,?)",
        (user_id, message[:300], now_iso()),
    )


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return error("Sign in to continue.", 401)
        user = get_db().execute(
            "SELECT id,email,full_name,role,active FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()
        if not user or not user["active"]:
            session.clear()
            return error("This account is unavailable. Contact an administrator.", 403)
        g.user = user
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            token = request.headers.get("X-CSRF-Token", "")
            if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
                return error("Your session token expired. Refresh and try again.", 403)
        return fn(*args, **kwargs)
    return wrapped


def roles(*allowed):
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in allowed:
                return error("You do not have permission to do that.", 403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def body_json():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def valid_coords(lat, lon):
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat, lon = float(lat), float(lon)
        if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon
    except (TypeError, ValueError):
        return None


def safe_hospital(row):
    data = dict(row)
    data["specialties"] = json.loads(data.get("specialties") or "[]")
    data["facilities"] = json.loads(data.get("facilities") or "[]")
    data["is_sample"] = bool(data.get("is_sample"))
    data["verified"] = bool(data.get("verified"))
    data["emergency_services"] = bool(data.get("emergency_services"))
    return data


def safe_doctor(row):
    data = dict(row)
    data["languages"] = json.loads(data.get("languages") or "[]")
    data["is_sample"] = bool(data.get("is_sample"))
    return data


def valid_photo_url(value):
    return not value or bool(
        re.fullmatch(r"/api/doctor-photos/[a-f0-9]{32}\.(?:png|jpe?g)", value)
    )


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def http_json(url, method="GET", data=None, timeout=8):
    headers = {"User-Agent": "NearbyCareAcademic/1.0 (local healthcare project)"}
    payload = None
    if data is not None:
        payload = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def geocode(query):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query[:180], "format": "jsonv2", "limit": 1}
    )
    result = http_json(url)
    return result[0] if result else None


def specialist_for(symptoms):
    text = " ".join(symptoms).lower()
    rules = [
        (("child health concern",), "Pediatrician",
         "A pediatrician is a suitable first point of contact for a child's health concern."),
        (("pregnancy concern",), "Gynecologist",
         "A gynecologist can assess pregnancy-related or reproductive-health concerns."),
        (("chest pain", "shortness of breath", "palpitations"), "Cardiologist",
         "Heart or breathing-related symptoms are commonly assessed by a cardiologist."),
        (("rash", "itching", "skin changes"), "Dermatologist",
         "Skin-related symptoms are commonly assessed by a dermatologist."),
        (("headache", "dizziness", "numbness", "seizure"), "Neurologist",
         "Neurological symptoms are commonly assessed by a neurologist."),
        (("joint pain", "back pain", "bone pain", "injury"), "Orthopedist",
         "Bone, joint, and injury symptoms are commonly assessed by an orthopedist."),
        (("fever", "cough", "sore throat", "stomach pain", "fatigue"), "General Physician",
         "A general physician can assess common symptoms and advise on next steps."),
    ]
    for terms, specialty, explanation in rules:
        if any(term in text for term in terms):
            return specialty, explanation
    return "General Physician", "A general physician is a reasonable starting point for the symptoms selected."


EMERGENCY_TERMS = {
    "chest pain", "difficulty breathing", "shortness of breath", "severe bleeding",
    "unconscious", "seizure", "face drooping", "suicidal thoughts",
}


def get_doctors(db, hospital_id=None):
    query = """SELECT d.*, a.status AS availability, a.updated_at AS availability_updated_at,
               h.name AS hospital_name, h.is_sample AS hospital_is_sample,
               h.verified AS hospital_verified
               FROM doctors d JOIN hospitals h ON h.id=d.hospital_id
               LEFT JOIN doctor_availability a ON a.doctor_id=d.id"""
    params = ()
    if hospital_id is not None:
        query += " WHERE d.hospital_id=?"
        params = (hospital_id,)
    query += " ORDER BY d.name"
    return [safe_doctor(row) for row in db.execute(query, params).fetchall()]


def nearby_osm(lat, lon):
    query = f"""[out:json][timeout:8];
      (node["amenity"~"^(hospital|clinic)$"](around:15000,{lat},{lon});
       way["amenity"~"^(hospital|clinic)$"](around:15000,{lat},{lon});
       relation["amenity"~"^(hospital|clinic)$"](around:15000,{lat},{lon}););
      out center tags;"""
    url = "https://overpass-api.de/api/interpreter"
    result = http_json(url, "POST", {"data": query}, timeout=12)
    hospitals = []
    for item in result.get("elements", [])[:60]:
        tags = item.get("tags", {})
        coords = item.get("center", item)
        if not coords.get("lat") or not coords.get("lon") or not tags.get("name"):
            continue
        hospitals.append({
            "id": "osm-" + str(item["id"]),
            "name": tags["name"][:140],
            "address": ", ".join(filter(None, [
                tags.get("addr:street"), tags.get("addr:suburb"),
                tags.get("addr:city"), tags.get("addr:postcode"),
            ])) or "Address not listed in OpenStreetMap",
            "area": tags.get("addr:suburb", ""),
            "city": tags.get("addr:city", ""),
            "latitude": coords["lat"],
            "longitude": coords["lon"],
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "specialties": [],
            "facilities": [],
            "emergency_services": tags.get("emergency") == "yes",
            "verified": False,
            "is_sample": False,
            "source": "OpenStreetMap community data",
            "rating": None,
            "rating_count": 0,
            "doctors": [],
            "osm_url": "https://www.openstreetmap.org/" + item["type"] + "/" + str(item["id"]),
        })
    return hospitals


def nearby_nominatim(lat, lon):
    radius = 0.18
    viewbox = f"{lon - radius},{lat + radius},{lon + radius},{lat - radius}"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": "hospital",
        "format": "jsonv2",
        "limit": 30,
        "viewbox": viewbox,
        "bounded": 1,
        "addressdetails": 1,
        "extratags": 1,
    })
    result = http_json(url)
    hospitals = []
    for item in result if isinstance(result, list) else []:
        category, kind = item.get("category") or item.get("class"), item.get("type")
        if category != "amenity" or kind not in {"hospital", "clinic"} or not item.get("name"):
            continue
        extratags = item.get("extratags") or {}
        latitude, longitude = valid_coords(item.get("lat"), item.get("lon")) or (None, None)
        if latitude is None or haversine_km(lat, lon, latitude, longitude) > 15:
            continue
        osm_type = item.get("osm_type", "")
        osm_url = f"https://www.openstreetmap.org/{osm_type}/{item.get('osm_id')}" if osm_type else ""
        hospitals.append({
            "id": f"osm-{osm_type}-{item.get('osm_id')}",
            "name": item["name"][:140],
            "address": item.get("display_name", "Address not listed in OpenStreetMap"),
            "area": "",
            "city": "",
            "latitude": latitude,
            "longitude": longitude,
            "phone": extratags.get("phone", ""),
            "specialties": [],
            "facilities": [],
            "emergency_services": extratags.get("emergency") == "yes",
            "verified": False,
            "is_sample": False,
            "source": "OpenStreetMap Nominatim community data",
            "rating": None,
            "rating_count": 0,
            "doctors": [],
            "osm_url": osm_url,
        })
    return hospitals


def rank_hospitals(hospitals, lat, lon, specialty, emergency):
    for hospital in hospitals:
        distance = haversine_km(lat, lon, float(hospital["latitude"]), float(hospital["longitude"]))
        hospital["distance_km"] = round(distance, 1)
        doctors = hospital.get("doctors", [])
        available = [d for d in doctors if d.get("availability") == "Available"]
        matching = bool(specialty) and (
            any(
                specialty.lower() in str(value).lower() or str(value).lower() in specialty.lower()
                for value in hospital.get("specialties", [])
            )
            or any(specialty.lower() in d["specialty"].lower() for d in doctors)
        )
        distance_score = max(0, 30 * (1 - min(distance, 30) / 30))
        score = 25 * matching + distance_score + 20 * bool(available)
        score += 10 * bool(hospital.get("emergency_services") and emergency)
        score += 10 * bool(hospital.get("verified"))
        if hospital.get("rating") is not None and hospital.get("rating_count"):
            score += 10 * float(hospital["rating"]) / 5
        hospital["match_score"] = round(min(score, 100))
        reasons = []
        if matching:
            reasons.append("lists the suggested specialty")
        if available:
            reasons.append("has a provider marked available in its staff-updated directory")
        reasons.append(f"{distance:.1f} km straight-line distance")
        if emergency and hospital.get("emergency_services"):
            reasons.append("lists emergency services")
        if hospital.get("verified"):
            reasons.append("hospital profile is verified")
        if hospital.get("rating") is not None and hospital.get("rating_count"):
            reasons.append(
                f"{hospital['rating']}/5 from {hospital['rating_count']} visit-verified ratings"
            )
        hospital["recommendation_reasons"] = reasons
    return sorted(hospitals, key=lambda h: (-h["match_score"], h["distance_km"], h["name"].lower()))


def discover_hospitals(db, coords, specialty=None, emergency=False):
    lat, lon = coords
    local = [safe_hospital(row) for row in db.execute(
        "SELECT * FROM hospitals WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall() if haversine_km(lat, lon, row["latitude"], row["longitude"]) <= 15]
    for hospital in local:
        hospital["doctors"] = get_doctors(db, hospital["id"])
        feedback = db.execute(
            """SELECT ROUND(AVG(rating),1) AS rating,COUNT(*) AS rating_count FROM feedback
               WHERE hospital_id=? AND verified_visit=1""", (hospital["id"],)
        ).fetchone()
        hospital["rating"] = feedback["rating"]
        hospital["rating_count"] = feedback["rating_count"]
    warning = None
    try:
        osm = nearby_osm(lat, lon)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        osm = []
        app.logger.warning("OpenStreetMap Overpass lookup failed: %s", exc)
    if not osm:
        try:
            osm = nearby_nominatim(lat, lon)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            warning = "OpenStreetMap discovery is temporarily unavailable; showing saved platform listings only."
            app.logger.warning("OpenStreetMap Nominatim fallback failed: %s", exc)
    return rank_hospitals(local + osm, lat, lon, specialty, emergency), warning


@app.errorhandler(413)
def too_large(_error):
    return error("Upload exceeds the 5 MB limit.", 413)


@app.get("/")
def home():
    return send_file(ROOT / "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "database": "sqlite"})


@app.post("/api/auth/register")
def register():
    data = body_json()
    name, email, password = str(data.get("full_name", "")).strip(), str(data.get("email", "")).strip().lower(), data.get("password", "")
    phone = str(data.get("phone", "")).strip()
    if not 2 <= len(name) <= 100 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return error("Enter a valid name and email address.")
    if not re.fullmatch(r"[+()\d .-]{7,20}", phone) or sum(char.isdigit() for char in phone) < 7:
        return error("Enter a valid phone number.")
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        return error("Password must contain 10–128 characters.")
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users(email,password_hash,full_name,phone,role,created_at) VALUES(?,?,?,?,?,?)",
            (email, generate_password_hash(password), name, phone, "patient", now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return error("An account with that email already exists.", 409)
    session.clear()
    session["user_id"] = cur.lastrowid
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({"user": {"id": cur.lastrowid, "email": email, "full_name": name, "role": "patient"},
                    "csrf_token": session["csrf_token"]}), 201


@app.post("/api/auth/login")
def login():
    data = body_json()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    expected_role = data.get("expected_role")
    if not isinstance(expected_role, str) or expected_role not in {"patient", "hospital", "admin"}:
        return error("Choose a valid account type.")
    row = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if (not row or not row["active"] or row["role"] != expected_role
            or not isinstance(password, str) or not check_password_hash(row["password_hash"], password)):
        return error("Email or password is incorrect.", 401)
    session.clear()
    session["user_id"] = row["id"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    audit("Sign in")
    return jsonify({"user": {"id": row["id"], "email": row["email"], "full_name": row["full_name"], "role": row["role"]},
                    "csrf_token": session["csrf_token"]})


@app.get("/api/me")
@login_required
def me():
    return jsonify({"user": dict(g.user), "csrf_token": session["csrf_token"]})


@app.post("/api/auth/logout")
@login_required
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/location/search")
@login_required
def location_search():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return error("Enter a city or area name.")
    try:
        place = geocode(query)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return error("Location search is temporarily unavailable; try GPS or another search.", 502)
    if not place:
        return error("No matching location found.")
    return jsonify({
        "latitude": float(place["lat"]), "longitude": float(place["lon"]),
        "display_name": place.get("display_name", query),
        "source": "OpenStreetMap Nominatim",
    })


@app.post("/api/location/reverse")
@login_required
def reverse_location():
    data = body_json()
    coords = valid_coords(data.get("latitude"), data.get("longitude"))
    if not coords:
        return error("Valid latitude and longitude are required.")
    lat, lon = coords
    try:
        result = http_json("https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
            {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 16}
        ))
        address = result.get("address", {})
        area = address.get("suburb") or address.get("neighbourhood") or address.get("village") or ""
        city = address.get("city") or address.get("town") or address.get("county") or ""
        display = ", ".join(filter(None, [area, city])) or result.get("display_name", "")
        return jsonify({"latitude": lat, "longitude": lon, "area": area, "city": city,
                        "display_name": display, "source": "OpenStreetMap Nominatim"})
    except (urllib.error.URLError, TimeoutError, ValueError):
        return jsonify({"latitude": lat, "longitude": lon, "area": "", "city": "",
                        "display_name": f"{lat:.4f}, {lon:.4f} (place name unavailable)",
                        "source": "Browser GPS coordinates"})


@app.post("/api/recommendations")
@roles("patient")
def recommendations():
    data = body_json()
    symptoms = data.get("symptoms")
    coords = valid_coords(data.get("latitude"), data.get("longitude"))
    try:
        duration = int(data.get("duration_days"))
        severity = int(data.get("severity"))
    except (TypeError, ValueError):
        return error("Enter a valid symptom duration and severity.")
    allowed_symptoms = {
        "chest pain", "difficulty breathing", "shortness of breath", "severe bleeding",
        "unconscious", "seizure", "face drooping", "suicidal thoughts",
        "child health concern", "pregnancy concern",
        "fever", "cough", "sore throat", "stomach pain", "fatigue", "rash",
        "itching", "skin changes", "headache", "dizziness", "numbness",
        "joint pain", "back pain", "bone pain", "injury", "palpitations",
    }
    if not isinstance(symptoms, list) or not symptoms or any(
        not isinstance(symptom, str) or symptom not in allowed_symptoms for symptom in symptoms
    ):
        return error("Select at least one symptom from the list.")
    if not coords or not 1 <= duration <= 3650 or not 1 <= severity <= 5:
        return error("Location, duration (1–3650 days) and severity (1–5) are required.")
    specialty, explanation = specialist_for(symptoms)
    emergency = any(s in EMERGENCY_TERMS for s in symptoms)
    db = get_db()
    db.execute(
        """INSERT INTO symptoms(patient_id,symptoms_json,duration_days,severity,specialist,emergency,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (g.user["id"], json.dumps(symptoms), duration, severity, specialty, int(emergency), now_iso()),
    )
    db.commit()
    ranked, external_error = discover_hospitals(db, coords, specialty, emergency)
    return jsonify({
        "specialist": specialty, "explanation": explanation, "emergency": emergency,
        "emergency_message": (
            "A selected symptom may indicate an emergency. This tool cannot assess urgency. "
            "Call your local emergency number or go to the nearest emergency department now; do not wait for online recommendations."
        ) if emergency else "",
        "score_explanation": "Match score (0–100): specialty match 25, distance up to 25, staff-reported available provider 20, emergency services when relevant 10, verified profile 10, and visit-verified patient ratings up to 10. Straight-line distance is not driving distance; missing data earns no points.",
        "hospitals": ranked, "warning": external_error,
    })


@app.get("/api/hospitals/nearby")
@roles("patient")
def nearby_hospitals():
    coords = valid_coords(request.args.get("latitude"), request.args.get("longitude"))
    if not coords:
        return error("Valid latitude and longitude are required.")
    hospitals, warning = discover_hospitals(get_db(), coords)
    return jsonify({
        "specialist": None,
        "explanation": "No symptom information was shared. Results are ranked by distance and available verified listing data only.",
        "emergency": False,
        "score_explanation": "No specialist was selected; specialty matching is excluded. Score uses straight-line distance, staff-reported availability, listed emergency services, profile verification and visit-verified patient ratings. Unknown information earns no points.",
        "hospitals": hospitals,
        "warning": warning,
    })


@app.get("/api/doctors")
@roles("patient", "hospital", "admin")
def doctors():
    hospital_id = request.args.get("hospital_id")
    if hospital_id and not str(hospital_id).isdigit():
        return error("Invalid hospital ID.")
    return jsonify({"doctors": get_doctors(get_db(), int(hospital_id) if hospital_id else None),
                    "updated_at": now_iso()})


@app.get("/api/doctors/updates")
@roles("patient", "hospital", "admin")
def doctor_updates():
    since = request.args.get("since", "")
    if len(since) > 40:
        return error("Invalid update timestamp.")
    rows = get_db().execute(
        """SELECT d.id,d.hospital_id,d.name,d.specialty,a.status,a.updated_at,
           h.name AS hospital_name FROM doctors d
           LEFT JOIN doctor_availability a ON a.doctor_id=d.id
           JOIN hospitals h ON h.id=d.hospital_id
           WHERE a.updated_at>? ORDER BY a.updated_at DESC LIMIT 200""", (since,)
    ).fetchall()
    return jsonify({"updates": [dict(row) for row in rows], "server_time": now_iso()})


@app.post("/api/appointments")
@roles("patient")
def book_appointment():
    data = body_json()
    try:
        hospital_id, doctor_id = int(data.get("hospital_id")), int(data.get("doctor_id"))
        appt_date = date.fromisoformat(str(data.get("date", "")))
        appt_time = datetime.strptime(str(data.get("time", "")), "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        return error("Choose a valid hospital, doctor, date and time.")
    if appt_date < date.today() or appt_date > date.today() + timedelta(days=180):
        return error("Choose a date between today and the next 180 days.")
    if appt_date == date.today() and appt_time <= datetime.now().strftime("%H:%M"):
        return error("Choose a future time for today's appointment.")
    db = get_db()
    doctor = db.execute(
        """SELECT d.*,a.status AS availability,h.name AS hospital_name,
           h.verified AS hospital_verified,h.is_sample AS hospital_is_sample
           FROM doctors d JOIN hospitals h ON h.id=d.hospital_id
           LEFT JOIN doctor_availability a ON a.doctor_id=d.id
           WHERE d.id=? AND d.hospital_id=?""", (doctor_id, hospital_id)
    ).fetchone()
    if not doctor:
        return error("That doctor is not listed at the selected hospital.", 404)
    if not doctor["hospital_verified"] and not doctor["hospital_is_sample"]:
        return error("This hospital profile is awaiting verification and cannot accept platform bookings.")
    if doctor["availability"] != "Available":
        return error("This doctor is not currently marked Available by hospital staff.")
    try:
        cur = db.execute(
            """INSERT INTO appointments(patient_id,hospital_id,doctor_id,appointment_date,
               appointment_time,status,note,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (g.user["id"], hospital_id, doctor_id, appt_date.isoformat(), appt_time,
             "pending", str(data.get("note", ""))[:500], now_iso()),
        )
        hospital_owner = db.execute(
            "SELECT owner_id FROM hospitals WHERE id=?", (doctor["hospital_id"],)
        ).fetchone()
        if hospital_owner and hospital_owner["owner_id"]:
            notify(hospital_owner["owner_id"], f"New appointment request from {g.user['full_name']}.")
        notify(g.user["id"], f"Appointment request sent to {doctor['hospital_name']}.")
        db.commit()
    except sqlite3.IntegrityError:
        return error("That time slot has already been booked. Choose another time.", 409)
    audit("Appointment requested")
    return jsonify({"id": cur.lastrowid, "status": "pending",
                    "message": "Appointment request sent; it is not confirmed until the hospital accepts it."}), 201


@app.get("/api/appointments")
@roles("patient")
def patient_appointments():
    db = get_db()
    rows = db.execute(
        """SELECT a.*,d.name AS doctor_name,d.specialty,h.name AS hospital_name,h.address AS hospital_address
           FROM appointments a JOIN doctors d ON d.id=a.doctor_id
           JOIN hospitals h ON h.id=a.hospital_id
           WHERE a.patient_id=? ORDER BY a.appointment_date DESC,a.appointment_time DESC""",
        (g.user["id"],),
    ).fetchall()
    reminder_cutoff = datetime.now() + timedelta(hours=24)
    for row in rows:
        if row["status"] not in {"accepted", "rescheduled"} or row["reminder_sent_at"]:
            continue
        try:
            appointment_at = datetime.fromisoformat(f"{row['appointment_date']}T{row['appointment_time']}")
        except ValueError:
            continue
        if datetime.now() <= appointment_at <= reminder_cutoff:
            db.execute("UPDATE appointments SET reminder_sent_at=? WHERE id=? AND reminder_sent_at IS NULL",
                       (now_iso(), row["id"]))
            if db.execute("SELECT changes()").fetchone()[0]:
                notify(g.user["id"], f"Reminder: appointment with {row['doctor_name']} at {row['hospital_name']} is within 24 hours.")
    db.commit()
    return jsonify({"appointments": [dict(row) for row in rows]})


@app.patch("/api/appointments/<int:appointment_id>")
@roles("patient")
def cancel_appointment(appointment_id):
    db = get_db()
    row = db.execute("SELECT * FROM appointments WHERE id=? AND patient_id=?",
                     (appointment_id, g.user["id"])).fetchone()
    if not row:
        return error("Appointment not found.", 404)
    if row["status"] not in {"pending", "accepted", "rescheduled"}:
        return error("This appointment can no longer be cancelled.")
    db.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appointment_id,))
    owner = get_db().execute("SELECT owner_id FROM hospitals WHERE id=?", (row["hospital_id"],)).fetchone()
    if owner and owner["owner_id"]:
        notify(owner["owner_id"], f"Appointment #{appointment_id} was cancelled by the patient.")
    db.commit()
    return jsonify({"ok": True, "status": "cancelled"})


@app.get("/api/profile")
@roles("patient")
def profile():
    db = get_db()
    history = db.execute("SELECT * FROM medical_history WHERE patient_id=? ORDER BY id DESC",
                         (g.user["id"],)).fetchall()
    favs = db.execute(
        """SELECT h.* FROM favourites f JOIN hospitals h ON h.id=f.hospital_id
           WHERE f.patient_id=? ORDER BY f.created_at DESC""", (g.user["id"],)
    ).fetchall()
    prescriptions = db.execute(
        "SELECT id,original_name,uploaded_at FROM prescriptions WHERE patient_id=? ORDER BY id DESC",
        (g.user["id"],),
    ).fetchall()
    notifications = db.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30", (g.user["id"],)
    ).fetchall()
    return jsonify({"history": [dict(r) for r in history],
                    "favourites": [safe_hospital(r) for r in favs],
                    "prescriptions": [dict(r) for r in prescriptions],
                    "notifications": [dict(r) for r in notifications],
                    "account": {"full_name": g.user["full_name"], "email": g.user["email"]}})


@app.put("/api/profile")
@roles("patient")
def update_patient_profile():
    data = body_json()
    full_name = str(data.get("full_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    if not 2 <= len(full_name) <= 100 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return error("Enter a valid name and email address.")
    db = get_db()
    if data.get("new_password"):
        user = db.execute("SELECT password_hash FROM users WHERE id=?", (g.user["id"],)).fetchone()
        current = data.get("current_password", "")
        new_password = data["new_password"]
        if not isinstance(current, str) or not check_password_hash(user["password_hash"], current):
            return error("Current password is incorrect.")
        if not isinstance(new_password, str) or not 10 <= len(new_password) <= 128:
            return error("New password must contain 10–128 characters.")
        password_hash = generate_password_hash(new_password)
        password_sql = ",password_hash=?"
        password_params = (password_hash,)
    else:
        password_sql, password_params = "", ()
    try:
        db.execute(
            f"UPDATE users SET full_name=?,email=?{password_sql} WHERE id=?",
            (full_name, email) + password_params + (g.user["id"],),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return error("An account with that email already exists.", 409)
    audit("Patient profile updated")
    return jsonify({"user": {"id": g.user["id"], "full_name": full_name, "email": email}})


@app.post("/api/medical-history")
@roles("patient")
def add_medical_history():
    data = body_json()
    condition = str(data.get("condition", "")).strip()
    if not 2 <= len(condition) <= 120:
        return error("Condition must contain 2–120 characters.")
    cur = get_db().execute(
        """INSERT INTO medical_history(patient_id,condition,details,allergies,created_at)
           VALUES(?,?,?,?,?)""",
        (g.user["id"], condition, str(data.get("details", ""))[:1000],
         str(data.get("allergies", ""))[:500], now_iso()),
    )
    get_db().commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.post("/api/prescriptions")
@roles("patient")
def upload_prescription():
    file = request.files.get("file")
    if not file or not file.filename:
        return error("Choose a prescription file.")
    name = secure_filename(file.filename)
    extension = Path(name).suffix.lower()
    if extension not in {".pdf", ".png", ".jpg", ".jpeg"}:
        return error("Only PDF, PNG or JPEG files are allowed.")
    content = file.read(UPLOAD_LIMIT + 1)
    if not content or len(content) > UPLOAD_LIMIT:
        return error("File is empty or exceeds the 5 MB limit.")
    stored = secrets.token_urlsafe(24) + extension
    path = UPLOAD_DIR / stored
    with path.open("wb") as target:
        target.write(content)
    cur = get_db().execute(
        "INSERT INTO prescriptions(patient_id,original_name,stored_name,uploaded_at) VALUES(?,?,?,?)",
        (g.user["id"], name[:180], stored, now_iso()),
    )
    get_db().commit()
    return jsonify({"id": cur.lastrowid, "original_name": name}), 201


@app.post("/api/hospital/doctors/<int:doctor_id>/photo")
@roles("hospital")
def upload_doctor_photo(doctor_id):
    hospital = owned_hospital()
    doctor = get_db().execute(
        "SELECT * FROM doctors WHERE id=? AND hospital_id=?",
        (doctor_id, hospital["id"] if hospital else -1),
    ).fetchone()
    if not doctor:
        return error("Doctor not found in your hospital.", 404)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return error("Choose a PNG or JPEG doctor photograph.")
    extension = Path(secure_filename(upload.filename)).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg"}:
        return error("Doctor photographs must be PNG or JPEG.")
    content = upload.read(UPLOAD_LIMIT + 1)
    valid_image = (
        (extension == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"))
    )
    if not content or len(content) > UPLOAD_LIMIT or not valid_image:
        return error("The selected file is empty, too large, or is not a valid PNG/JPEG image.")
    filename = secrets.token_hex(16) + extension
    (PHOTO_DIR / filename).write_bytes(content)
    old_url = doctor["photo_url"]
    photo_url = f"/api/doctor-photos/{filename}"
    get_db().execute("UPDATE doctors SET photo_url=?,updated_at=? WHERE id=?",
                     (photo_url, now_iso(), doctor_id))
    get_db().commit()
    if old_url and old_url.startswith("/api/doctor-photos/"):
        old_name = old_url.rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", old_name):
            old_path = PHOTO_DIR / old_name
            if old_path.is_file():
                old_path.unlink()
    return jsonify({"photo_url": photo_url})


@app.post("/api/hospital/photo")
@roles("hospital")
def upload_hospital_photo():
    hospital = owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return error("Choose a PNG or JPEG hospital photograph.")
    extension = Path(secure_filename(upload.filename)).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg"}:
        return error("Hospital photographs must be PNG or JPEG.")
    content = upload.read(UPLOAD_LIMIT + 1)
    valid_image = (
        (extension == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"))
    )
    if not content or len(content) > UPLOAD_LIMIT or not valid_image:
        return error("The selected file is empty, too large, or is not a valid PNG/JPEG image.")
    filename = secrets.token_hex(16) + extension
    (PHOTO_DIR / filename).write_bytes(content)
    old_url = hospital["photo_url"]
    photo_url = f"/api/hospital-photos/{filename}"
    get_db().execute("UPDATE hospitals SET photo_url=? WHERE id=?", (photo_url, hospital["id"]))
    get_db().commit()
    if old_url and old_url.startswith("/api/hospital-photos/"):
        old_name = old_url.rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", old_name):
            old_path = PHOTO_DIR / old_name
            if old_path.is_file():
                old_path.unlink()
    return jsonify({"photo_url": photo_url})


@app.get("/api/hospital-photos/<filename>")
def hospital_photo(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", filename):
        return error("Photo not found.", 404)
    path = PHOTO_DIR / filename
    if not path.is_file():
        return error("Photo not found.", 404)
    response = send_file(path, conditional=True, max_age=3600)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/api/doctor-photos/<filename>")
def doctor_photo(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", filename):
        return error("Photo not found.", 404)
    path = PHOTO_DIR / filename
    if not path.is_file():
        return error("Photo not found.", 404)
    response = send_file(path, conditional=True, max_age=3600)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/api/prescriptions/<int:prescription_id>")
@login_required
def download_prescription(prescription_id):
    db = get_db()
    row = db.execute("SELECT * FROM prescriptions WHERE id=?", (prescription_id,)).fetchone()
    if not row:
        return error("Prescription not found.", 404)
    if g.user["role"] not in {"admin"} and row["patient_id"] != g.user["id"]:
        return error("You do not have permission to access this file.", 403)
    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        return error("Prescription file is missing.", 404)
    return send_file(path, as_attachment=True, download_name=row["original_name"])


@app.post("/api/favourites/<int:hospital_id>")
@roles("patient")
def favourite(hospital_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM hospitals WHERE id=?", (hospital_id,)).fetchone():
        return error("Saved hospital not found.", 404)
    db.execute("INSERT OR IGNORE INTO favourites(patient_id,hospital_id,created_at) VALUES(?,?,?)",
               (g.user["id"], hospital_id, now_iso()))
    db.commit()
    return jsonify({"saved": True})


@app.delete("/api/favourites/<int:hospital_id>")
@roles("patient")
def unfavourite(hospital_id):
    get_db().execute("DELETE FROM favourites WHERE patient_id=? AND hospital_id=?",
                     (g.user["id"], hospital_id))
    get_db().commit()
    return jsonify({"saved": False})


@app.post("/api/feedback")
@roles("patient")
def submit_feedback():
    data = body_json()
    try:
        hospital_id, rating = int(data.get("hospital_id")), int(data.get("rating"))
    except (TypeError, ValueError):
        return error("Choose a hospital and a 1–5 rating.")
    db = get_db()
    hospital = db.execute("SELECT id FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hospital or not 1 <= rating <= 5:
        return error("Choose a listed hospital and a rating from 1 to 5.")
    today = date.today().isoformat()
    current_time = datetime.now().strftime("%H:%M")
    verified = bool(db.execute(
        """SELECT 1 FROM appointments WHERE patient_id=? AND hospital_id=?
           AND status IN ('accepted','rescheduled')
           AND (appointment_date<? OR (appointment_date=? AND appointment_time<=?)) LIMIT 1""",
        (g.user["id"], hospital_id, today, today, current_time),
    ).fetchone())
    try:
        db.execute(
            """INSERT INTO feedback(patient_id,hospital_id,rating,comment,verified_visit,created_at)
               VALUES(?,?,?,?,?,?)""",
            (g.user["id"], hospital_id, rating, str(data.get("comment", ""))[:1000],
             int(verified), now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return error("You have already submitted visit-verified feedback for this hospital.", 409)
    return jsonify({"ok": True, "verified_visit": verified}), 201


@app.post("/api/reports")
@roles("patient")
def report_information():
    data = body_json()
    message = str(data.get("message", "")).strip()
    if not 5 <= len(message) <= 1000:
        return error("Report details must contain 5–1000 characters.")
    hospital_id = data.get("hospital_id")
    try:
        hospital_id = int(hospital_id) if hospital_id else None
    except (ValueError, TypeError):
        return error("Invalid hospital.")
    cur = get_db().execute(
        """INSERT INTO reports(patient_id,hospital_id,message,created_at) VALUES(?,?,?,?)""",
        (g.user["id"], hospital_id, message, now_iso()),
    )
    get_db().commit()
    return jsonify({"id": cur.lastrowid, "message": "Report sent to the platform team."}), 201


def owned_hospital():
    return get_db().execute("SELECT * FROM hospitals WHERE owner_id=?", (g.user["id"],)).fetchone()


@app.get("/api/hospital/dashboard")
@roles("hospital")
def hospital_dashboard():
    db = get_db()
    hospital = owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    doctors_list = get_doctors(db, hospital["id"])
    appointments = db.execute(
        """SELECT a.*,u.full_name AS patient_name,d.name AS doctor_name,d.specialty
           FROM appointments a JOIN users u ON u.id=a.patient_id
           JOIN doctors d ON d.id=a.doctor_id WHERE a.hospital_id=?
           ORDER BY a.appointment_date,a.appointment_time LIMIT 100""", (hospital["id"],)
    ).fetchall()
    counts = {status: sum(1 for d in doctors_list if d.get("availability") == status) for status in AVAILABILITY}
    today_count = db.execute(
        "SELECT COUNT(*) FROM appointments WHERE hospital_id=? AND appointment_date=?",
        (hospital["id"], date.today().isoformat()),
    ).fetchone()[0]
    return jsonify({"hospital": safe_hospital(hospital), "doctors": doctors_list,
                    "appointments": [dict(a) for a in appointments],
                    "stats": {"total_doctors": len(doctors_list), "availability": counts,
                              "today_appointments": today_count}})


@app.put("/api/hospital/profile")
@roles("hospital")
def update_hospital():
    data, hospital = body_json(), owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    name = str(data.get("name", "")).strip()
    has_lat = data.get("latitude") not in (None, "")
    has_lon = data.get("longitude") not in (None, "")
    if has_lat != has_lon:
        return error("Provide both latitude and longitude, or leave both unchanged.")
    coords = valid_coords(data.get("latitude"), data.get("longitude")) if has_lat else None
    if has_lat and not coords:
        return error("Enter valid latitude and longitude coordinates.")
    if not 2 <= len(name) <= 140:
        return error("Hospital name must contain 2–140 characters.")
    try:
        specialties = data.get("specialties", [])
        facilities = data.get("facilities", [])
        if not isinstance(specialties, list) or not isinstance(facilities, list):
            raise ValueError()
        specialties = [str(x)[:80] for x in specialties[:30]]
        facilities = [str(x)[:100] for x in facilities[:30]]
    except ValueError:
        return error("Specialties and facilities must be lists.")
    db = get_db()
    db.execute(
        """UPDATE hospitals SET name=?,address=?,area=?,city=?,latitude=?,longitude=?,phone=?,
           specialties=?,facilities=?,emergency_services=? WHERE id=?""",
        (name, str(data.get("address", ""))[:300], str(data.get("area", ""))[:100],
         str(data.get("city", ""))[:100], coords[0] if coords else hospital["latitude"],
         coords[1] if coords else hospital["longitude"], str(data.get("phone", ""))[:30],
         json.dumps(specialties), json.dumps(facilities), int(bool(data.get("emergency_services"))),
         hospital["id"]),
    )
    db.commit()
    audit("Hospital profile updated")
    return jsonify({"ok": True})


@app.post("/api/hospital/doctors")
@roles("hospital")
def create_doctor():
    data, hospital = body_json(), owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    name, specialty = str(data.get("name", "")).strip(), str(data.get("specialty", "")).strip()
    try:
        years = int(data.get("experience_years", 0))
    except (ValueError, TypeError):
        return error("Experience must be a whole number.")
    languages = data.get("languages", [])
    if not 2 <= len(name) <= 120 or len(specialty) < 2 or not 0 <= years <= 80 or not isinstance(languages, list):
        return error("Enter valid doctor details and language list.")
    photo_url = str(data.get("photo_url", ""))[:500]
    if not valid_photo_url(photo_url):
        return error("Doctor photos must be uploaded through the hospital dashboard.")
    db = get_db()
    cur = db.execute(
        """INSERT INTO doctors(hospital_id,name,qualification,specialty,experience_years,languages,
           consultation_timings,photo_url,is_sample,updated_at) VALUES(?,?,?,?,?,?,?,?,0,?)""",
        (hospital["id"], name, str(data.get("qualification", ""))[:160], specialty[:80],
         years, json.dumps([str(x)[:40] for x in languages[:10]]),
         str(data.get("consultation_timings", ""))[:200], photo_url, now_iso()),
    )
    db.execute("INSERT INTO doctor_availability(doctor_id,status,updated_at,updated_by) VALUES(?,?,?,?)",
               (cur.lastrowid, "Unavailable", now_iso(), g.user["id"]))
    db.commit()
    return jsonify({"id": cur.lastrowid, "availability": "Unavailable"}), 201


@app.put("/api/hospital/doctors/<int:doctor_id>")
@roles("hospital")
def update_doctor(doctor_id):
    data, db, hospital = body_json(), get_db(), owned_hospital()
    row = db.execute("SELECT * FROM doctors WHERE id=? AND hospital_id=?",
                     (doctor_id, hospital["id"] if hospital else -1)).fetchone()
    if not row:
        return error("Doctor not found in your hospital.", 404)
    status = data.get("availability")
    if status is not None and status not in AVAILABILITY:
        return error("Choose Available, Busy, On Break or Unavailable.")
    languages = data.get("languages", json.loads(row["languages"] or "[]"))
    if not isinstance(languages, list):
        return error("Languages must be a list.")
    fields = {
        "name": str(data.get("name", row["name"])).strip()[:120],
        "qualification": str(data.get("qualification", row["qualification"]))[:160],
        "specialty": str(data.get("specialty", row["specialty"])).strip()[:80],
        "consultation_timings": str(data.get("consultation_timings", row["consultation_timings"]))[:200],
        "photo_url": str(data.get("photo_url", row["photo_url"]))[:500],
        "languages": json.dumps([str(x)[:40] for x in languages[:10]]),
    }
    try:
        years = int(data.get("experience_years", row["experience_years"]))
    except (ValueError, TypeError):
        return error("Experience must be a whole number.")
    if len(fields["name"]) < 2 or len(fields["specialty"]) < 2 or not 0 <= years <= 80:
        return error("Enter valid doctor details.")
    if not valid_photo_url(fields["photo_url"]):
        return error("Doctor photos must be uploaded through the hospital dashboard.")
    db.execute(
        """UPDATE doctors SET name=?,qualification=?,specialty=?,experience_years=?,languages=?,
           consultation_timings=?,photo_url=?,updated_at=? WHERE id=?""",
        (fields["name"], fields["qualification"], fields["specialty"], years, fields["languages"],
         fields["consultation_timings"], fields["photo_url"], now_iso(), doctor_id),
    )
    if status is not None:
        db.execute(
            """INSERT INTO doctor_availability(doctor_id,status,updated_at,updated_by) VALUES(?,?,?,?)
               ON CONFLICT(doctor_id) DO UPDATE SET status=excluded.status,
               updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            (doctor_id, status, now_iso(), g.user["id"]),
        )
        db.execute(
            """INSERT INTO notifications(user_id,message,created_at)
               SELECT DISTINCT patient_id,?,? FROM appointments
               WHERE doctor_id=? AND status IN ('pending','accepted','rescheduled')""",
            (f"Availability updated for {fields['name']}: {status}.", now_iso(), doctor_id),
        )
    db.commit()
    audit("Doctor profile or availability updated")
    return jsonify({"ok": True, "availability": status})


@app.delete("/api/hospital/doctors/<int:doctor_id>")
@roles("hospital")
def delete_doctor(doctor_id):
    hospital = owned_hospital()
    db = get_db()
    if not hospital or not db.execute(
        "SELECT 1 FROM doctors WHERE id=? AND hospital_id=?", (doctor_id, hospital["id"])
    ).fetchone():
        return error("Doctor not found in your hospital.", 404)
    if db.execute("SELECT 1 FROM appointments WHERE doctor_id=? AND status IN ('pending','accepted','rescheduled')",
                  (doctor_id,)).fetchone():
        return error("Doctor has active appointment requests; resolve them before removing the doctor.", 409)
    db.execute("DELETE FROM doctors WHERE id=?", (doctor_id,))
    db.commit()
    return jsonify({"ok": True})


@app.patch("/api/hospital/appointments/<int:appointment_id>")
@roles("hospital")
def update_appointment(appointment_id):
    data, db, hospital = body_json(), get_db(), owned_hospital()
    row = db.execute("SELECT * FROM appointments WHERE id=? AND hospital_id=?",
                     (appointment_id, hospital["id"] if hospital else -1)).fetchone()
    if not row:
        return error("Appointment not found.", 404)
    if row["status"] not in {"pending", "accepted", "rescheduled"}:
        return error("This appointment is no longer active.")
    status = data.get("status")
    if status not in {"accepted", "rejected", "rescheduled"}:
        return error("Choose accept, reject or reschedule.")
    target_date, target_time = row["appointment_date"], row["appointment_time"]
    if status == "rescheduled":
        try:
            target_date = date.fromisoformat(str(data.get("date", ""))).isoformat()
            target_time = datetime.strptime(str(data.get("time", "")), "%H:%M").strftime("%H:%M")
            if date.fromisoformat(target_date) < date.today():
                return error("Rescheduled appointments must be in the future.")
            if target_date == date.today().isoformat() and target_time <= datetime.now().strftime("%H:%M"):
                return error("Choose a future time for today's rescheduled appointment.")
        except ValueError:
            return error("Choose a valid reschedule date and time.")
    try:
        db.execute(
            "UPDATE appointments SET status=?,appointment_date=?,appointment_time=? WHERE id=?",
            (status, target_date, target_time, appointment_id),
        )
    except sqlite3.IntegrityError:
        return error("That time slot is already booked. Choose another.", 409)
    notify(row["patient_id"], f"Appointment #{appointment_id} was {status} by the hospital.")
    db.commit()
    return jsonify({"ok": True, "status": status, "date": target_date, "time": target_time})


@app.get("/api/notifications")
@login_required
def notifications():
    rows = get_db().execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (g.user["id"],)
    ).fetchall()
    return jsonify({"notifications": [dict(r) for r in rows]})


@app.get("/api/admin/dashboard")
@roles("admin")
def admin_dashboard():
    db = get_db()
    counts = {
        "patients": db.execute("SELECT COUNT(*) FROM users WHERE role='patient'").fetchone()[0],
        "hospital_accounts": db.execute("SELECT COUNT(*) FROM users WHERE role='hospital'").fetchone()[0],
        "hospitals": db.execute("SELECT COUNT(*) FROM hospitals").fetchone()[0],
        "unverified": db.execute("SELECT COUNT(*) FROM hospitals WHERE verified=0").fetchone()[0],
        "appointments": db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0],
        "pending_appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE status='pending'").fetchone()[0],
        "accepted_appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE status='accepted'").fetchone()[0],
        "rescheduled_appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE status='rescheduled'").fetchone()[0],
        "open_reports": db.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0],
    }
    hospitals = db.execute(
        """SELECT h.*,u.email AS owner_email,u.full_name AS owner_name,
           (SELECT status FROM hospital_verification v WHERE v.hospital_id=h.id ORDER BY v.id DESC LIMIT 1) AS verification_status
           FROM hospitals h LEFT JOIN users u ON u.id=h.owner_id ORDER BY h.verified,h.id DESC"""
    ).fetchall()
    users = db.execute(
        "SELECT id,email,full_name,role,active,created_at FROM users ORDER BY id DESC LIMIT 100"
    ).fetchall()
    reports = db.execute(
        """SELECT r.*,u.full_name AS patient_name,h.name AS hospital_name FROM reports r
           JOIN users u ON u.id=r.patient_id LEFT JOIN hospitals h ON h.id=r.hospital_id
           ORDER BY r.id DESC LIMIT 100"""
    ).fetchall()
    activity = db.execute(
        """SELECT a.*,u.full_name FROM activity_log a LEFT JOIN users u ON u.id=a.user_id
           ORDER BY a.id DESC LIMIT 30"""
    ).fetchall()
    doctor_rows = db.execute(
        """SELECT d.id,d.name,d.specialty,d.is_sample,a.status AS availability,
           a.updated_at AS availability_updated_at,h.name AS hospital_name,h.verified AS hospital_verified
           FROM doctors d JOIN hospitals h ON h.id=d.hospital_id
           LEFT JOIN doctor_availability a ON a.doctor_id=d.id ORDER BY h.name,d.name"""
    ).fetchall()
    return jsonify({"stats": counts, "hospitals": [safe_hospital(h) | {
        "owner_email": h["owner_email"], "owner_name": h["owner_name"],
        "verification_status": h["verification_status"],
    } for h in hospitals], "doctors": [dict(d) for d in doctor_rows],
                    "users": [dict(u) for u in users],
                    "reports": [dict(r) for r in reports], "activity": [dict(a) for a in activity]})


@app.post("/api/admin/hospitals")
@roles("admin")
def admin_create_hospital():
    data = body_json()
    name, email, staff, password = (
        str(data.get("name", "")).strip(), str(data.get("email", "")).strip().lower(),
        str(data.get("staff_name", "")).strip(), data.get("password", ""),
    )
    if len(name) < 2 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(staff) < 2:
        return error("Enter a valid hospital name, staff name and email.")
    if not isinstance(password, str) or len(password) < 10:
        return error("Temporary password must contain at least 10 characters.")
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users(email,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
            (email, generate_password_hash(password), staff, "hospital", now_iso()),
        )
        coords = valid_coords(data.get("latitude"), data.get("longitude"))
        hospital_cur = db.execute(
            """INSERT INTO hospitals(owner_id,name,address,area,city,latitude,longitude,phone,
               specialties,facilities,emergency_services,source,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cur.lastrowid, name, str(data.get("address", ""))[:300],
             str(data.get("area", ""))[:100], str(data.get("city", ""))[:100],
             coords[0] if coords else None, coords[1] if coords else None,
             str(data.get("phone", ""))[:30], "[]", "[]", 0, "admin registered", now_iso()),
        )
        db.execute(
            "INSERT INTO hospital_verification(hospital_id,admin_id,status,updated_at) VALUES(?,?,?,?)",
            (hospital_cur.lastrowid, g.user["id"], "pending", now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return error("An account with that email already exists.", 409)
    return jsonify({"hospital_id": hospital_cur.lastrowid, "message": "Hospital account created and awaiting verification."}), 201


@app.patch("/api/admin/hospitals/<int:hospital_id>")
@roles("admin")
def verify_hospital(hospital_id):
    data = body_json()
    verified = data.get("verified")
    if not isinstance(verified, bool):
        return error("Set verified to true or false.")
    db = get_db()
    hospital = db.execute("SELECT id FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hospital:
        return error("Hospital not found.", 404)
    status = "verified" if verified else "pending"
    db.execute("UPDATE hospitals SET verified=? WHERE id=?", (int(verified), hospital_id))
    db.execute(
        """INSERT INTO hospital_verification(hospital_id,admin_id,status,note,updated_at)
           VALUES(?,?,?,?,?)""",
        (hospital_id, g.user["id"], status, str(data.get("note", ""))[:500], now_iso()),
    )
    owner = db.execute("SELECT owner_id FROM hospitals WHERE id=?", (hospital_id,)).fetchone()["owner_id"]
    if owner:
        notify(owner, "Your hospital profile was verified." if verified else "Your hospital verification is pending.")
    db.commit()
    return jsonify({"verified": verified})


@app.patch("/api/admin/users/<int:user_id>")
@roles("admin")
def set_user_active(user_id):
    active = body_json().get("active")
    if not isinstance(active, bool):
        return error("Set active to true or false.")
    if user_id == g.user["id"]:
        return error("You cannot disable your own admin account.")
    cur = get_db().execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
    get_db().commit()
    if not cur.rowcount:
        return error("Account not found.", 404)
    return jsonify({"active": active})


@app.patch("/api/admin/reports/<int:report_id>")
@roles("admin")
def resolve_report(report_id):
    status = body_json().get("status")
    if status not in {"open", "resolved"}:
        return error("Choose open or resolved.")
    cur = get_db().execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))
    get_db().commit()
    if not cur.rowcount:
        return error("Report not found.", 404)
    return jsonify({"status": status})


@app.get("/api/route")
@roles("patient")
def route():
    start = valid_coords(request.args.get("start_lat"), request.args.get("start_lon"))
    end = valid_coords(request.args.get("end_lat"), request.args.get("end_lon"))
    profile = request.args.get("profile", "driving")
    if not start or not end:
        return error("Valid origin and destination coordinates are required.")
    if profile != "driving":
        return error("The configured public OSRM service provides driving routes only. Use Google Maps for other travel modes.", 422)
    coordinates = f"{start[1]},{start[0]};{end[1]},{end[0]}"
    url = f"https://router.project-osrm.org/route/v1/driving/{coordinates}?" + urllib.parse.urlencode(
        {"overview": "full", "geometries": "geojson", "steps": "false"}
    )
    try:
        result = http_json(url, timeout=12)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return error("Road routing service is temporarily unavailable. Google Maps navigation is still available.", 502)
    if result.get("code") != "Ok" or not result.get("routes"):
        return error("No driving route was found for these locations. Try Google Maps navigation.", 404)
    route_data = result["routes"][0]
    return jsonify({"distance_km": round(route_data["distance"] / 1000, 1),
                    "duration_minutes": round(route_data["duration"] / 60),
                    "geometry": route_data["geometry"], "source": "OSRM public routing service"})


@app.cli.command("init-db")
def init_db_command():
    init_db()
    print(f"SQLite database initialized: {DATABASE}")


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
