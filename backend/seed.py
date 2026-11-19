from . import auth, config, db, rag

USERS = [
    {"username": "dr.patel", "password": "clinician123", "role": "clinician", "full_name": "Dr. S. Patel"},
    {"username": "admin", "password": "admin123", "role": "admin", "full_name": "System Administrator"},
    {"username": "front.desk", "password": "staff123", "role": "staff", "full_name": "Front Desk Staff"},
]

PATIENTS = [
    {"mrn": "SYN-1001", "name": "J. Rivera", "dob": "1978-04-12", "sex": "Male"},
    {"mrn": "SYN-1002", "name": "M. Chen", "dob": "1991-09-30", "sex": "Female"},
    {"mrn": "SYN-1003", "name": "A. Okafor", "dob": "1965-02-18", "sex": "Male"},
]

PATIENT_ROLES = ["clinician", "admin"]
GLOBAL_ROLES = ["clinician", "admin", "staff"]

DOCUMENTS = [
    {"mrn": "SYN-1001", "file": "patient_record_sample.txt", "title": "Patient Record - J. Rivera", "doc_type": "patient_record"},
    {"mrn": "SYN-1001", "file": "lab_report_sample.txt", "title": "Lab Report - J. Rivera", "doc_type": "lab_report"},
    {"mrn": "SYN-1001", "file": "prescription_sample.txt", "title": "Prescription History - J. Rivera", "doc_type": "prescription"},
    {"mrn": "SYN-1001", "file": "discharge_summary_sample.txt", "title": "Discharge Summary - J. Rivera", "doc_type": "discharge_summary"},
    {"mrn": "SYN-1002", "file": "patient2_record_sample.txt", "title": "Patient Record - M. Chen", "doc_type": "patient_record"},
    {"mrn": "SYN-1002", "file": "patient2_lab_report_sample.txt", "title": "Pulmonary Function Report - M. Chen", "doc_type": "lab_report"},
    {"mrn": "SYN-1002", "file": "patient2_prescription_sample.txt", "title": "Prescription History - M. Chen", "doc_type": "prescription"},
    {"mrn": "SYN-1003", "file": "patient3_record_sample.txt", "title": "Patient Record - A. Okafor", "doc_type": "patient_record"},
    {"mrn": "SYN-1003", "file": "patient3_lab_report_sample.txt", "title": "Cardiology/Lab Report - A. Okafor", "doc_type": "lab_report"},
    {"mrn": "SYN-1003", "file": "patient3_prescription_sample.txt", "title": "Prescription History - A. Okafor", "doc_type": "prescription"},
    {"mrn": None, "file": "hospital_guideline_sample.txt", "title": "Hospital Guideline - Sepsis Screening Protocol", "doc_type": "guideline"},
]


def seed_if_empty():
    db.init_db()
    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM patients").fetchone()["c"]
        if count > 0:
            return {"seeded": False}

        for u in USERS:
            password_hash, salt = auth.hash_password(u["password"])
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, role, full_name) VALUES (%s, %s, %s, %s, %s)",
                (u["username"], password_hash, salt, u["role"], u["full_name"]),
            )

        mrn_to_id = {}
        for p in PATIENTS:
            row = conn.execute(
                "INSERT INTO patients (mrn, name, dob, sex) VALUES (%s, %s, %s, %s) RETURNING id",
                (p["mrn"], p["name"], p["dob"], p["sex"]),
            ).fetchone()
            mrn_to_id[p["mrn"]] = row["id"]

        documents_indexed = 0
        for d in DOCUMENTS:
            doc_path = config.DOCUMENTS_DIR / d["file"]
            if not doc_path.exists():
                continue
            text = doc_path.read_text()
            patient_db_id = mrn_to_id.get(d["mrn"]) if d["mrn"] else None
            roles = GLOBAL_ROLES if patient_db_id is None else PATIENT_ROLES
            rag.ingest_document(
                conn=conn,
                patient_db_id=patient_db_id,
                title=d["title"],
                doc_type=d["doc_type"],
                allowed_roles=roles,
                text=text,
                source_filename=d["file"],
                uploaded_by="seed",
            )
            documents_indexed += 1

        return {"seeded": True, "users": len(USERS), "patients": len(PATIENTS), "documents": documents_indexed}
