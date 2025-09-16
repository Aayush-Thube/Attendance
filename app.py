# app.py

import os
import hashlib
from datetime import datetime, date, timedelta
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from geopy.distance import geodesic
from streamlit_js_eval import get_geolocation
import openpyxl
from io import BytesIO
import sqlite3
import time
import secrets
from typing import Optional

# Set page config at the very top - must be first Streamlit command
st.set_page_config("Attendance","🕒")
from zoneinfo import ZoneInfo
# Initialize cookies manager
from streamlit_cookies_manager import EncryptedCookieManager
cookies = EncryptedCookieManager(
    prefix="attendance_app",
    password="attendance_secret_key_2024"
)

# ---------------------------
# CONFIG
# ---------------------------
ADMIN_PHONES = ["8080042473"]
DEFAULT_DASHBOARD_PW = "32193"
SEED_OFFICES = [
    {"OfficeName": "CSMT", "Latitude": 18.94358359403972, "Longitude": 72.83826109487124, "RadiusMeters": 350},
    {"OfficeName": "Thane", "Latitude": 19.236363706991003, "Longitude": 72.98719749815108, "RadiusMeters": 350},
    {"OfficeName": "Nerul", "Latitude": 19.044282739911402, "Longitude": 73.01426940651511, "RadiusMeters": 350},
]

DATA_FILE = "attendance_system.xlsx"
ROW_LIMIT = 1_048_000
MONTH_LIMIT = 10

# ---------------------------
# UTILITIES
# ---------------------------
def hash_pw(pw: str) -> str:
    return hashlib.sha256(str(pw).encode()).hexdigest()

def init_workbook():
    if os.path.exists(DATA_FILE): return
    with pd.ExcelWriter(DATA_FILE, engine="openpyxl") as writer:
        # Users
        df_users = pd.DataFrame(columns=["PhoneNumber","Name","Departments","PasswordHash","Role"])
        for i, ph in enumerate(ADMIN_PHONES):
            df_users.loc[len(df_users)] = [ph, f"Admin{i+1}", "Management Team", hash_pw(DEFAULT_DASHBOARD_PW), "Admin"]
        df_users.to_excel(writer, sheet_name="users", index=False)
        # Attendance
        pd.DataFrame(columns=["Date","Name","PhoneNumber","IN","OUT","WFH","Leave","Departments","Office"])\
            .to_excel(writer, sheet_name="attendance_1", index=False)
        # Offices
        offs = pd.DataFrame(SEED_OFFICES)
        offs.to_excel(writer, sheet_name="offices", index=False)
        # Departments
        pd.DataFrame({"DepartmentGroup":["Management Team",]}).to_excel(writer, sheet_name="departments", index=False)
        # Settings
        s = pd.DataFrame({"Key":["whitelist"], "Value":[",".join(ADMIN_PHONES)]})
        s.to_excel(writer, sheet_name="settings", index=False)
        # Edit logs
        pd.DataFrame(columns=["DateTime","EditedByPhone","EditedByName","TargetPhone","Date","Field","OldValue","NewValue","Reason"])\
            .to_excel(writer, sheet_name="attendance_edits", index=False)

def read_sheet(sheet):
    try:
        return pd.read_excel(DATA_FILE, sheet_name=sheet, engine="openpyxl", dtype=str).fillna("")
    except Exception as e:
        st.error(f"Error reading sheet '{sheet}': {e}")
        # Return empty DataFrame if sheet doesn't exist
        return pd.DataFrame()

def write_sheet(sheet, df):
    try:
        with pd.ExcelWriter(DATA_FILE, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            df.to_excel(writer, sheet_name=sheet, index=False)
    except Exception as e:
        st.error(f"Error writing to Excel: {e}")

def get_latest_attendance_sheet():
    book = openpyxl.load_workbook(DATA_FILE)
    # Only consider sheets with numeric suffix like attendance_1, attendance_2, ...
    numeric_sheets = []
    for s in book.sheetnames:
        if s.startswith("attendance_"):
            suffix = s.split("_")[-1]
            if suffix.isdigit():
                numeric_sheets.append((int(suffix), s))
    if not numeric_sheets:
        # Fallback: create the first attendance sheet if missing
        first = "attendance_1"
        # Ensure the sheet is created if it doesn't exist
        try:
            pd.read_excel(DATA_FILE, sheet_name=first)
        except Exception:
            write_sheet(first, pd.DataFrame(columns=["Date","Name","PhoneNumber","IN","OUT","WFH","Leave","Departments","Office"]))
        return first
    numeric_sheets.sort(key=lambda t: t[0])
    return numeric_sheets[-1][1]

def cleanup_and_rotate(df):
    cutoff = date.today() - timedelta(days=MONTH_LIMIT*30)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date # Ensure 'Date' column is datetime.date
    df = df[df["Date"] >= cutoff]
    if len(df) > ROW_LIMIT:
        new_sheet = f"attendance_{int(get_latest_attendance_sheet().split('_')[1])+1}"
        write_sheet(new_sheet, pd.DataFrame(columns=df.columns))
        df = df.iloc[-ROW_LIMIT:]
    return df

# ---------------------------
# Storage Class
# ---------------------------
class ExcelStorage:
    def init(self): init_workbook()
    def get_user(self, phone):
        df=read_sheet("users"); r=df[df["PhoneNumber"]==str(phone)]
        return None if r.empty else r.iloc[0].to_dict()
    def add_user(self,u): df=read_sheet("users"); df=pd.concat([df,pd.DataFrame([u])],ignore_index=True); write_sheet("users",df)
    def update_user(self,phone,updates):
        df=read_sheet("users")
        if str(phone) not in df["PhoneNumber"].values: return False
        for k,v in updates.items():
            if k not in df.columns: df[k]=""
            df.loc[df["PhoneNumber"]==str(phone),k]=v
        write_sheet("users",df); return True
    def check_password(self,phone,pw): u=self.get_user(phone); return u and u["PasswordHash"]==hash_pw(pw)

    def mark_attendance(self, phone, name, deps, action, office=None):
        try:
            action = str(action).strip().upper()
            sheet = get_latest_attendance_sheet()
            df = read_sheet(sheet)
            df = cleanup_and_rotate(df)
            LOCAL_TZ = ZoneInfo("Asia/Kolkata")
            now_local = datetime.now(LOCAL_TZ)
            day = now_local.date()
            nowt = now_local.strftime("%H:%M:%S")
            
            idxs = df[(df["PhoneNumber"] == str(phone)) & (df["Date"] == day)].index

            if len(idxs):
                idx = idxs[0]
            else:
                new = {
                    "Date": day,
                    "Name": name,
                    "PhoneNumber": phone,
                    "Departments": deps,
                    "IN": "",
                    "OUT": "",
                    "WFH": "No",
                    "Leave": "No",
                    "Office": ""
                }
                df = pd.concat([df, pd.DataFrame([new])], ignore_index=True)
                idx = df.index[-1]

            # --- Office Handling ---
            # --- Office Handling ---
            prev_office = str(df.at[idx, "Office"]).strip()
            offices = [o.strip() for o in prev_office.split(",") if o.strip()] if prev_office else []

            if office and office != "-":
                if office not in offices:
                    offices.append(office)
                df.at[idx, "Office"] = ",".join(offices)


        # --- Independent Actions ---
            if action == "IN":
                df.at[idx, "IN"] = nowt   # ✅ Updates only IN
                if df.at[idx, "WFH"] == "":
                    df.at[idx, "WFH"] = "No"
                if df.at[idx, "Leave"] == "":
                    df.at[idx, "Leave"] = "No"

            elif action == "OUT":
                df.at[idx, "OUT"] = nowt  # ✅ Updates only OUT
                if df.at[idx, "WFH"] == "":
                    df.at[idx, "WFH"] = "No"
                if df.at[idx, "Leave"] == "":
                    df.at[idx, "Leave"] = "No"

            elif action == "WFH IN":
                df.at[idx, "IN"] = nowt
                df.at[idx, "WFH"] = "Yes"

            elif action == "WFH OUT":
                df.at[idx, "OUT"] = nowt
                df.at[idx, "WFH"] = "Yes"

            elif action == "LEAVE":
                df.at[idx, "Leave"] = "Yes"
            else:
                return False, f"Invalid action: {action}"

        # Fill defaults if missing
            if not df.at[idx, "WFH"]:
                df.at[idx, "WFH"] = "No"
            if not df.at[idx, "Leave"]:
                df.at[idx, "Leave"] = "No"

            write_sheet(sheet, df)
            return True, "Recorded"

        except Exception as e:
            st.error(f"Error in mark_attendance: {e}")
            return False, f"Error: {e}"


    def get_attendance(self): return read_sheet(get_latest_attendance_sheet())
    def get_offices(self): return read_sheet("offices")
    def add_office(self,n,lat,lon,r): df=self.get_offices(); df.loc[len(df)]=[n,lat,lon,r]; write_sheet("offices",df)
    def delete_office(self,n): df=self.get_offices(); df=df[df["OfficeName"]!=n]; write_sheet("offices",df)
    def get_departments(self): return read_sheet("departments")
    def add_department(self,g): df=self.get_departments(); df.loc[len(df)]=[g]; write_sheet("departments",df)
    def delete_department(self,g): df=self.get_departments(); df=df[df["DepartmentGroup"]!=g]; write_sheet("departments",df)
    def get_setting(self,key): df=read_sheet("settings"); r=df[df["Key"]==key]; return "" if r.empty else str(r.iloc[0]["Value"])
    def set_setting(self,key,val):
        df = read_sheet("settings")
        if key in df["Key"].values:
            df.loc[df["Key"]==key, "Value"] = str(val)
        else:
            df = pd.concat([df, pd.DataFrame([{ "Key": key, "Value": str(val)}])], ignore_index=True)
        write_sheet("settings", df)
    def append_edit(self,e): df=read_sheet("attendance_edits"); df=pd.concat([df,pd.DataFrame([e])],ignore_index=True); write_sheet("attendance_edits",df)

    def update_attendance_fields(self, phone: str, date_str: str, updates: dict):
        sheet = get_latest_attendance_sheet()
        df = read_sheet(sheet)
        # Normalize Date to string for comparison
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
        target_date = pd.to_datetime(date_str, errors="coerce").date() if not isinstance(date_str, date) else date_str
        mask = (df["PhoneNumber"].astype(str) == str(phone)) & (df["Date"] == target_date)
        for k, v in updates.items():
            if k not in df.columns:
                df[k] = ""
            df.loc[mask, k] = v
        write_sheet(sheet, df)
        return True

class SqlStorage:
    def __init__(self, db_path: str = "attendance.db"):
        self.db_path = db_path

    def init(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        # Users
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
          PhoneNumber TEXT PRIMARY KEY,
          Name TEXT,
          Departments TEXT,
          PasswordHash TEXT,
          Role TEXT
        )""")
        # Attendance
        cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
          Date TEXT,
          Name TEXT,
          PhoneNumber TEXT,
          IN_TIME TEXT,
          OUT_TIME TEXT,
          WFH TEXT,
          Leave TEXT,
          Departments TEXT,
          Office TEXT,
          PRIMARY KEY (Date, PhoneNumber)
        )""")
        # Offices
        cur.execute("""
        CREATE TABLE IF NOT EXISTS offices (
          OfficeName TEXT PRIMARY KEY,
          Latitude REAL,
          Longitude REAL,
          RadiusMeters REAL
        )""")
        # Departments
        cur.execute("""
        CREATE TABLE IF NOT EXISTS departments (
          DepartmentGroup TEXT PRIMARY KEY
        )""")
        # Settings
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
          Key TEXT PRIMARY KEY,
          Value TEXT
        )""")
        # Edit logs
        cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance_edits (
          DateTime TEXT,
          EditedByPhone TEXT,
          EditedByName TEXT,
          TargetPhone TEXT,
          Date TEXT,
          Field TEXT,
          OldValue TEXT,
          NewValue TEXT,
          Reason TEXT
        )""")

        # Seed admin phones in users and whitelist if empty
        for i, ph in enumerate(ADMIN_PHONES):
            cur.execute("INSERT OR IGNORE INTO users (PhoneNumber, Name, Departments, PasswordHash, Role) VALUES (?,?,?,?,?)",
                        (ph, f"Admin{i+1}", "Management Team", hash_pw(DEFAULT_DASHBOARD_PW), "Admin"))
        # Seed whitelist setting
        cur.execute("INSERT OR IGNORE INTO settings (Key, Value) VALUES (?, ?)", ("whitelist", ",".join(ADMIN_PHONES)))
        # Seed departments default
        cur.execute("INSERT OR IGNORE INTO departments (DepartmentGroup) VALUES (?)", ("Management Team",))

        con.commit(); con.close()

    # User APIs
    def get_user(self, phone):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT PhoneNumber, Name, Departments, PasswordHash, Role FROM users WHERE PhoneNumber=?", (str(phone),))
        row = cur.fetchone(); con.close()
        if not row: return None
        return {"PhoneNumber": row[0], "Name": row[1], "Departments": row[2], "PasswordHash": row[3], "Role": row[4]}

    def add_user(self, u):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO users (PhoneNumber, Name, Departments, PasswordHash, Role) VALUES (?,?,?,?,?)",
                    (u.get("PhoneNumber"), u.get("Name"), u.get("Departments",""), u.get("PasswordHash",""), u.get("Role","User")))
        con.commit(); con.close()

    def update_user(self, phone, updates):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        # Build dynamic update
        fields = []
        values = []
        for k in ["Name","Departments","PasswordHash","Role","PhoneNumber"]:
            if k in updates:
                fields.append(f"{ 'PhoneNumber' if k=='PhoneNumber' else k }=?")
                values.append(updates[k])
        if not fields:
            con.close(); return True
        values.append(str(phone))
        cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE PhoneNumber=?", tuple(values))
        changed = cur.rowcount
        con.commit(); con.close(); return changed > 0

    def check_password(self, phone, pw):
        u = self.get_user(phone)
        return u and u.get("PasswordHash") == hash_pw(pw)

    # Attendance APIs
    def mark_attendance(self, phone, name, deps, action, office=None):
        try:
            action = str(action).strip().upper()
            LOCAL_TZ = ZoneInfo("Asia/Kolkata")
            now_local = datetime.now(LOCAL_TZ)
            today_str = now_local.date().isoformat()
            nowt = now_local.strftime("%H:%M:%S")
            con = sqlite3.connect(self.db_path)
            cur = con.cursor()
            # Ensure row exists
            cur.execute("SELECT Date, Name, PhoneNumber, IN_TIME, OUT_TIME, WFH, Leave, Departments, Office FROM attendance WHERE Date=? AND PhoneNumber=?",
                        (today_str, str(phone)))
            row = cur.fetchone()
            if not row:
                cur.execute("INSERT INTO attendance (Date, Name, PhoneNumber, IN_TIME, OUT_TIME, WFH, Leave, Departments, Office) VALUES (?,?,?,?,?,?,?,?,?)",
                            (today_str, name, str(phone), "", "", "No", "No", deps, ""))

            # Office merge
            if office and office != "-":
                cur.execute("SELECT Office FROM attendance WHERE Date=? AND PhoneNumber=?", (today_str, str(phone)))
                prev = cur.fetchone()
                current = prev[0] if prev and prev[0] else ""
                parts = [p.strip() for p in current.split(',') if p and p.strip()]
                if office not in parts:
                    parts.append(office)
                new_off = ",".join(parts)
                cur.execute("UPDATE attendance SET Office=? WHERE Date=? AND PhoneNumber=?", (new_off, today_str, str(phone)))

            # Actions
            if action == "IN":
                cur.execute("UPDATE attendance SET IN_TIME=?, WFH=COALESCE(NULLIF(WFH,''),'No'), Leave=COALESCE(NULLIF(Leave,''),'No') WHERE Date=? AND PhoneNumber=?",
                            (nowt, today_str, str(phone)))
            elif action == "OUT":
                cur.execute("UPDATE attendance SET OUT_TIME=?, WFH=COALESCE(NULLIF(WFH,''),'No'), Leave=COALESCE(NULLIF(Leave,''),'No') WHERE Date=? AND PhoneNumber=?",
                            (nowt, today_str, str(phone)))
            elif action == "WFH IN":
                cur.execute("UPDATE attendance SET IN_TIME=?, WFH='Yes' WHERE Date=? AND PhoneNumber=?",
                            (nowt, today_str, str(phone)))
            elif action == "WFH OUT":
                cur.execute("UPDATE attendance SET OUT_TIME=?, WFH='Yes' WHERE Date=? AND PhoneNumber=?",
                            (nowt, today_str, str(phone)))
            elif action == "LEAVE":
                cur.execute("UPDATE attendance SET Leave='Yes' WHERE Date=? AND PhoneNumber=?", (today_str, str(phone)))
            else:
                con.close(); return False, f"Invalid action: {action}"

            con.commit(); con.close(); return True, "Recorded"
        except Exception as e:
            st.error(f"Error in mark_attendance (SQL): {e}")
            return False, f"Error: {e}"

    def get_attendance(self):
        con = sqlite3.connect(self.db_path)
        df = pd.read_sql_query("SELECT Date as Date, Name, PhoneNumber, IN_TIME as `IN`, OUT_TIME as `OUT`, WFH, Leave, Departments, Office FROM attendance", con)
        con.close(); return df.fillna("")

    # Offices / Departments / Settings / Edits
    def get_offices(self):
        con = sqlite3.connect(self.db_path)
        df = pd.read_sql_query("SELECT OfficeName, Latitude, Longitude, RadiusMeters FROM offices", con)
        con.close(); return df.fillna("")
    def add_office(self, n, lat, lon, r):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO offices (OfficeName, Latitude, Longitude, RadiusMeters) VALUES (?,?,?,?)", (n, lat, lon, r))
        con.commit(); con.close()
    def delete_office(self, n):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        cur.execute("DELETE FROM offices WHERE OfficeName=?", (n,))
        con.commit(); con.close()

    def get_departments(self):
        con = sqlite3.connect(self.db_path)
        df = pd.read_sql_query("SELECT DepartmentGroup FROM departments", con)
        con.close(); return df.fillna("")
    def add_department(self, g):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO departments (DepartmentGroup) VALUES (?)", (g,))
        con.commit(); con.close()
    def delete_department(self, g):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        cur.execute("DELETE FROM departments WHERE DepartmentGroup=?", (g,))
        con.commit(); con.close()

    def get_setting(self, key):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        cur.execute("SELECT Value FROM settings WHERE Key=?", (key,))
        row = cur.fetchone(); con.close()
        return "" if not row else str(row[0])
    def set_setting(self, key, val):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        cur.execute("INSERT INTO settings (Key, Value) VALUES (?, ?) ON CONFLICT(Key) DO UPDATE SET Value=excluded.Value", (key, str(val)))
        con.commit(); con.close()

    def append_edit(self, e):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        cur.execute("INSERT INTO attendance_edits (DateTime, EditedByPhone, EditedByName, TargetPhone, Date, Field, OldValue, NewValue, Reason) VALUES (?,?,?,?,?,?,?,?,?)",
                    (e.get("DateTime"), e.get("EditedByPhone"), e.get("EditedByName"), e.get("TargetPhone"), e.get("Date"), e.get("Field"), e.get("OldValue"), e.get("NewValue"), e.get("Reason")))
        con.commit(); con.close()

    def update_attendance_fields(self, phone: str, date_str: str, updates: dict):
        con = sqlite3.connect(self.db_path); cur = con.cursor()
        sets = []
        vals = []
        # Map keys to SQL columns
        mapping = {"IN": "IN_TIME", "OUT": "OUT_TIME", "WFH": "WFH", "Leave": "Leave", "Office": "Office"}
        for k, v in updates.items():
            col = mapping.get(k, k)
            sets.append(f"{col}=?")
            vals.append(v)
        if not sets:
            con.close(); return True
        vals.extend([date_str if isinstance(date_str, str) else date_str.isoformat(), str(phone)])
        cur.execute(f"UPDATE attendance SET {', '.join(sets)} WHERE Date=? AND PhoneNumber=?", tuple(vals))
        con.commit(); con.close(); return True

def get_storage_mode():
    try:
        if os.path.exists("storage_mode.txt"):
            with open("storage_mode.txt", "r", encoding="utf-8") as f:
                mode = f.read().strip().lower()
                if mode in ("sql", "sqlite", "db"):
                    return "sql"
    except Exception:
        pass
    return "excel"

if get_storage_mode() == "sql":
    storage = SqlStorage()
else:
    storage = ExcelStorage()
storage.init()

# Validate Excel file integrity
def validate_excel_file():
    try:
        # Try to read a sheet to check if file is valid
        test_df = pd.read_excel(DATA_FILE, sheet_name="users", engine="openpyxl")
        if test_df.empty:
            st.warning("Excel file appears to be empty, recreating...")
            init_workbook()
    except Exception as e:
        st.error(f"Excel file is corrupted: {e}")
        # Try to remove file, but don't fail if it's locked
        try:
            if os.path.exists(DATA_FILE):
                os.remove(DATA_FILE)
        except PermissionError:
            st.warning("File is locked by another process. Will try to recreate on next restart.")
            return
        except Exception:
            pass
        init_workbook()

# Check file integrity on startup
validate_excel_file()

# ---------------------------
# UI HELPERS
# ---------------------------
def nav_to(p): st.session_state.page=p; st.rerun()

# ---------------------------
# STREAMLIT APP
# ---------------------------
st.title("🕒 Attendance App")
if "page" not in st.session_state: st.session_state.page="login"
if "user" not in st.session_state: st.session_state.user=None
if "pending_wfh_confirm" not in st.session_state: st.session_state.pending_wfh_confirm=False
if "pending_wfh_office" not in st.session_state: st.session_state.pending_wfh_office=None
if "last_click_time" not in st.session_state: st.session_state.last_click_time=0
if "user_attempting_login" not in st.session_state: st.session_state.user_attempting_login=False

# Auto-login if remember cookie exists and no active user
# Only run auto-login on initial page load, not when user is actively logging in
if (st.session_state.page=="login" and 
    st.session_state.user is None and 
    not st.session_state.get("just_logged_out", False) and
    not st.session_state.get("user_attempting_login", False) and
    not st.session_state.get("login_page_visited", False)):
    
    if not cookies.ready():
        st.stop()
    
    try:
        remembered_phone = cookies.get("remembered_phone")
        if remembered_phone:
            u = storage.get_user(remembered_phone)
            if u:
                st.session_state.user = u
                st.session_state.page = "home"
                st.rerun()
    except Exception:
        pass

# Mark that login page has been visited to prevent auto-login on subsequent visits
if st.session_state.page == "login":
    st.session_state.login_page_visited = True

# Reset the just_logged_out flag after auto-login check
if st.session_state.get("just_logged_out", False):
    st.session_state.just_logged_out = False

# Reset user_attempting_login flag when not on login page
if st.session_state.page != "login":
    st.session_state.user_attempting_login = False

# LOGIN
def show_login():
    st.header("Login")
    
    phone=st.text_input("Phone")
    pw=st.text_input("Password",type="password")
    remember = st.checkbox("Remember me on this device")
    
    # Set flag when user starts entering credentials to prevent auto-login
    if phone.strip():
        st.session_state.user_attempting_login = True
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Login"):
            if storage.check_password(phone,pw):
                user = storage.get_user(phone)
                st.session_state.user = user
                st.session_state.user_attempting_login = False  # Reset flag on successful login
                
                # Handle remember me functionality
                if remember:
                    cookies["remembered_phone"] = phone
                    cookies.save()
                else:
                    # Clear remember cookie if not checked
                    if "remembered_phone" in cookies:
                        del cookies["remembered_phone"]
                        cookies.save()
                
                nav_to("home")
            else: 
                st.error("Invalid phone or password") # Corrected error message
    with col2:
        if st.button("Sign Up"):
            nav_to("signup")

# SIGNUP
def show_signup():
    st.header("Sign up")
    name=st.text_input("Name"); phone=st.text_input("Phone"); pw=st.text_input("Password",type="password")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Create Account"):
            # Check if phone is already in whitelist (admin)
            whitelist = [p.strip() for p in storage.get_setting("whitelist").split(",")]
            if phone in whitelist:
                st.error("This phone number is reserved for admin use. Please contact administrator.")
            elif storage.get_user(phone):
                st.error("User already exists")
            else: # This else was missing, causing the signup to not proceed
                u={"PhoneNumber":phone,"Name":name,"Departments":"","PasswordHash":hash_pw(pw),"Role":"User"}
                storage.add_user(u); st.session_state.user=u; nav_to("home")
    with col2:
        if st.button("Back to Login"):
            nav_to("login")

# HOME
def show_home():
    u=st.session_state.user; st.header(f"Hello, {u['Name']}")
    if st.button("Mark Attendance"): nav_to("mark")
    if st.button("Profile"): nav_to("profile")

    whitelist = [p.strip() for p in storage.get_setting("whitelist").split(",") if p.strip()]
    # Access to Admin Dashboard is controlled solely by whitelist
    if (u["PhoneNumber"] in whitelist):
        if st.button("Admin Dashboard"): nav_to("admin")

    if st.button("Logout"):
        # Clear all session state variables
        st.session_state.user = None
        st.session_state.pending_wfh_confirm = False
        st.session_state.pending_wfh_office = None
        st.session_state.last_click_time = 0
        st.session_state.user_geolocation = None
        st.session_state.geolocation_attempts = 0
        st.session_state.user_attempting_login = False
        st.session_state.login_page_visited = False  # Reset to allow auto-login on next visit
        st.session_state.just_logged_out = True  # Prevent immediate auto-login
        
        # Clear remember cookie on logout
        if "remembered_phone" in cookies:
            del cookies["remembered_phone"]
            cookies.save()
        
        # Force page refresh and redirect to login
        st.session_state.page = "login"
        st.success("✅ Successfully logged out!")
        st.rerun()

# MARK
def show_mark():
    u = st.session_state.user
    st.header("Mark Attendance")
    offices_df = storage.get_offices()
    office_names = offices_df["OfficeName"].tolist() if not offices_df.empty else []

    selected_office = st.selectbox("Select Office (skip for WFH)", options=["-"] + office_names)

    # Simple location check function
    def user_within_selected_office(user_location) -> bool:
        if selected_office == "-":
            return False
        if not user_location or "coords" not in user_location:
            return False

        row = offices_df[offices_df["OfficeName"] == selected_office].iloc[0]
        office_lat = float(row["Latitude"])
        office_lon = float(row["Longitude"])
        radius = float(row["RadiusMeters"])

        user_lat = user_location["coords"].get("latitude")
        user_lon = user_location["coords"].get("longitude")

        if user_lat is None or user_lon is None:
            return False

        distance_m = geodesic((user_lat, user_lon), (office_lat, office_lon)).meters
        return distance_m <= (radius + 150)  # 100m buffer

    col1, col2, col3, col4, col5 = st.columns(5)

    # Geolocation fetch with mobile-friendly handling
    if "user_geolocation" not in st.session_state:
        st.session_state.user_geolocation = None
    if "geolocation_attempts" not in st.session_state:
        st.session_state.geolocation_attempts = 0

    geolocation_placeholder = st.empty()
    
    # Try to get location with retries for mobile
    if st.session_state.user_geolocation is None and st.session_state.geolocation_attempts < 3:
        st.session_state.geolocation_attempts += 1
        st.session_state.user_geolocation = get_geolocation()
        
        if st.session_state.user_geolocation is None:
            if st.session_state.geolocation_attempts == 1:
                geolocation_placeholder.warning("⚠️ Getting your location... Please allow location access in your browser.")
            elif st.session_state.geolocation_attempts == 2:
                geolocation_placeholder.warning("⚠️ Still getting location... Make sure location services are enabled.")
            else:
                geolocation_placeholder.warning("⚠️ Location access needed. You can still mark attendance, but office verification will be skipped.")
        else:
            geolocation_placeholder.success("✅ Location detected!")
    elif st.session_state.user_geolocation is None:
        geolocation_placeholder.info("ℹ️ Location not available. You can still mark attendance.")
        
        # Add refresh location button for mobile users
        if st.button("🔄 Try Location Again", key="refresh_location"):
            st.session_state.user_geolocation = None
            st.session_state.geolocation_attempts = 0
            st.rerun()

    user_loc = st.session_state.user_geolocation

    # --- IN ---
    if col1.button("IN"):
        import time
        current_time = time.time()
        if current_time - st.session_state.last_click_time < 2:
            st.warning("Please wait a moment before clicking again...")
            st.session_state.user_geolocation = None
            return
        st.session_state.last_click_time = current_time

        if selected_office != "-":
            # Check if we have location data
            if user_loc and "coords" in user_loc:
                if user_within_selected_office(user_loc):
                    ok, msg = storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "IN", selected_office)
                    if ok:
                        storage.update_attendance_fields(u["PhoneNumber"],datetime.now(ZoneInfo("Asia/Kolkata")).date(),{"Office": selected_office})
                        st.success(f"✅ {msg} at {selected_office}")
                        st.rerun()
                    else:
                        st.error(msg)
                else:
                    # Location available but outside radius - show warning and offer options
                    st.warning(f"⚠️ You are not within the {selected_office} office radius. Please choose an option below:")
                    col_confirm1, col_confirm2 = st.columns(2)
                    with col_confirm1:
                        if st.button(f"Confirm IN at {selected_office}", key="confirm_office_in"):
                            ok, msg = storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "IN", selected_office)
                            if ok:
                                storage.update_attendance_fields(u["PhoneNumber"],datetime.now(ZoneInfo("Asia/Kolkata")).date(),{"Office": selected_office})
                                st.success(f"✅ {msg} at {selected_office}")
                                st.rerun()
                    with col_confirm2:
                        if st.button("Mark as WFH IN", key="wfh_in_fallback"):
                            st.success(storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "WFH IN")[1])
                            st.rerun()
            else:
                # No location data - allow manual confirmation
                col_confirm1, col_confirm2 = st.columns(2)
                with col_confirm1:
                    if st.button(f"Confirm IN at {selected_office}", key="confirm_office_in_no_loc"):
                        ok, msg = storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "IN", selected_office)
                        if ok:
                            storage.update_attendance_fields(u["PhoneNumber"], date.today(), {"Office": selected_office})
                            st.success(f"✅ {msg} at {selected_office}")
                            st.rerun()
                with col_confirm2:
                    if st.button("Mark as WFH IN", key="wfh_in_no_loc"):
                        st.success(storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "WFH IN")[1])
                        st.rerun()
        else:
            st.success(storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "WFH IN")[1])
            st.rerun()

    # --- OUT ---
    if col2.button("OUT"):
        st.session_state.pending_wfh_confirm = False
        st.session_state.pending_wfh_office = None

        if selected_office != "-":
            ok, msg = storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "OUT",selected_office if selected_office != "-" else None)

            if ok:
                storage.update_attendance_fields(u["PhoneNumber"], date.today(), {"Office": selected_office})
                st.success(f"✅ {msg} at {selected_office}")
                st.rerun()
            else:
                st.error(msg)
        else:
            st.success(storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "WFH OUT")[1])
            st.rerun()

    # --- Leave ---
    if col3.button("Leave"):
        st.session_state.pending_wfh_confirm = False
        st.session_state.pending_wfh_office = None
        st.success(storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "Leave",None))
        st.rerun()

    # --- WFH IN ---
    if col4.button("WFH IN"):
        st.session_state.pending_wfh_confirm = False
        st.session_state.pending_wfh_office = None
        st.success(storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "WFH IN",None))
        st.rerun()

    # --- WFH OUT ---
    if col5.button("WFH OUT"):
        st.session_state.pending_wfh_confirm = False
        st.session_state.pending_wfh_office = None
        st.success(storage.mark_attendance(u["PhoneNumber"], u["Name"], u["Departments"], "WFH OUT",None))
        st.rerun()

    if st.button("Back"):
        st.session_state.pending_wfh_confirm = False
        st.session_state.pending_wfh_office = None
        st.session_state.user_geolocation = None
        nav_to("home")


# PROFILE
def show_profile():
    u=st.session_state.user; st.header("Profile")
    # Editable fields
    new_name = st.text_input("Name", value=u.get("Name",""))
    new_phone = st.text_input("Phone", value=u.get("PhoneNumber",""))
    # Departments selection (store as comma-separated list)
    dept_df = storage.get_departments()
    dept_options = dept_df["DepartmentGroup"].dropna().unique().tolist() if not dept_df.empty else []

    if not dept_options:
        st.info("No departments found. Add one below to proceed.")
        new_dept_name = st.text_input("New Department Group")
        if st.button("Add Department Group") and new_dept_name.strip():
            storage.add_department(new_dept_name.strip())
            st.success("Department added")
            st.rerun()
    current_deps = [d.strip() for d in str(u.get("Departments","")) .split(",") if d.strip()]
    new_deps = st.multiselect("Departments", options=dept_options, default=[d for d in current_deps if d in dept_options])

    st.subheader("Change Password")
    pw1 = st.text_input("New Password", type="password")
    pw2 = st.text_input("Confirm New Password", type="password")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Save Changes"):
            # Validate phone uniqueness if changed
            original_phone = u["PhoneNumber"]
            updates = {"Name": new_name, "Departments": ",".join(new_deps)} # Phone number update handled separately
            
            # Handle password change
            if pw1 or pw2:
                if pw1 != pw2:
                    st.error("Passwords do not match")
                    return
                updates["PasswordHash"] = hash_pw(pw1)
            
            # Handle phone number change
            if new_phone != original_phone:
                existing = storage.get_user(new_phone)
                if existing and existing["PhoneNumber"] != original_phone: # Ensure it's not the current user's phone
                    st.error("Phone already registered to another user")
                    return
                updates["PhoneNumber"] = new_phone # Add new phone to updates if it changed

            # Apply updates
            if storage.update_user(original_phone, updates):
                # If phone number was changed, update the session user with the new phone
                if "PhoneNumber" in updates and updates["PhoneNumber"] != original_phone:
                    st.session_state.user = storage.get_user(updates["PhoneNumber"])
                else:
                    # Otherwise, just refresh the current user's data
                    st.session_state.user = storage.get_user(original_phone)
                st.success("Profile updated")
            else:
                st.error("Failed to update profile.")
    with col2:
        if st.button("Back"):
            nav_to("home")

# ADMIN
def show_admin():
    st.header("Admin Dashboard")
    # Always show Back at top for easy navigation
    top_back_col, _ = st.columns([1,6])
    with top_back_col:
        if st.button("Back", key="admin_back_top"):
            nav_to("home")
    tab1,tab2,tab3,tab4,tab5=st.tabs(["Attendance","Departments/Offices","Edit Logs","Settings","Edit Attendance"])

    with tab1:
        df_all = storage.get_attendance()
        if df_all.empty:
            st.info("No attendance records yet")
        else:
            st.subheader("Attendance Viewer & Export")
            df_all["Date"] = pd.to_datetime(df_all["Date"], errors="coerce").dt.date
            valid_dates = df_all["Date"].dropna()
            if valid_dates.empty:
                min_d = max_d = date.today()
            else:
                min_d = valid_dates.min()
                max_d = valid_dates.max()
            col_a, col_b = st.columns(2)
            with col_a:
                start_date = st.date_input("Start date", value=min_d)
            with col_b:
                end_date = st.date_input("End date", value=max_d)

            if start_date and end_date:
                mask = (df_all["Date"] >= start_date) & (df_all["Date"] <= end_date)
                df_view = df_all.loc[mask]
            else:
                df_view = df_all
            st.dataframe(df_view)


            # Downloads
            csv_bytes = df_view.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", data=csv_bytes, file_name=f"attendance_{start_date}_to_{end_date}.csv", mime="text/csv")

            buf = BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df_view.to_excel(writer, index=False, sheet_name="attendance")
            st.download_button("Download Excel", data=buf.getvalue(), file_name=f"attendance_{start_date}_to_{end_date}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with tab2:
        st.subheader("Departments")
        ddf = storage.get_departments()
        st.dataframe(ddf)
        col_ad, col_dd = st.columns(2)
        with col_ad:
            dept_new = st.text_input("Add Department Group")
            if st.button("Add Group") and dept_new.strip():
                storage.add_department(dept_new.strip())
                st.success("Department group added")
                st.rerun()
        with col_dd:
            if not ddf.empty:
                del_group = st.selectbox("Delete Department Group", ["-"] + ddf["DepartmentGroup"].unique().tolist())
                if st.button("Delete Group") and del_group and del_group != "-":
                    storage.delete_department(del_group)
                    st.warning("Department group deleted")
                    st.rerun()

        st.markdown("---")
        st.subheader("Offices (Locations)")
        odf = storage.get_offices()
        st.dataframe(odf)
        col_o1, col_o2 = st.columns(2)
        with col_o1:
            off_name = st.text_input("Office Name")
            lat = st.number_input("Latitude", value=0.0, format="%.6f")
            lon = st.number_input("Longitude", value=0.0, format="%.6f")
            rad = st.number_input("Radius (meters)", value=200, min_value=50, step=50)
            if st.button("Add / Update Office") and off_name.strip():
                # If exists, delete then add to update values
                try:
                    storage.delete_office(off_name.strip())
                except Exception:
                    pass # Ignore if office doesn't exist for deletion
                storage.add_office(off_name.strip(), lat, lon, rad)
                st.success("Office saved")
                st.rerun()
        with col_o2:
            if not odf.empty:
                del_off = st.selectbox("Delete Office", ["-"] + odf["OfficeName"].tolist())
                if st.button("Delete Selected Office") and del_off and del_off != "-":
                    storage.delete_office(del_off)
                    st.warning("Office deleted")
                    st.rerun()
    with tab3:
        st.subheader("Edit Logs")
        try:
            edit_df = read_sheet("attendance_edits")
            if not edit_df.empty:
                st.dataframe(edit_df)
            else:
                st.info("No edit logs yet")
        except Exception as e: # Catch specific exception if sheet is truly missing
            st.info(f"No edit logs sheet found or error reading: {e}")
    with tab4:
        st.subheader("Admin & Access Settings")
        # Whitelist management
        wl_raw = storage.get_setting("whitelist")
        wl_list = [p.strip() for p in wl_raw.split(",") if p.strip()] if wl_raw else []
        wl_text = st.text_area("Whitelist phone numbers (comma-separated)", value=",".join(wl_list))
        if st.button("Save Whitelist", key="save_whitelist"):
            storage.set_setting("whitelist", wl_text)
            st.success("Whitelist updated")

        st.markdown("---")
        st.subheader("Grant Access (adds to whitelist)")
        access_phone = st.text_input("Phone Number to Grant Access")
        access_name = st.text_input("Name (optional)")
        if st.button("Grant Access", key="grant_access") and access_phone.strip():
            # Ensure user exists
            existing = storage.get_user(access_phone.strip())
            if not existing:
                u = {"PhoneNumber": access_phone.strip(), "Name": access_name or f"User-{access_phone.strip()}", "Departments":"", "PasswordHash": hash_pw(DEFAULT_DASHBOARD_PW), "Role":"User"}
                storage.add_user(u)
            # Add to whitelist
            new_wl = [p.strip() for p in wl_text.split(",") if p.strip()]
            if access_phone.strip() not in new_wl:
                new_wl.append(access_phone.strip())
            storage.set_setting("whitelist", ",".join(new_wl))
            st.success("Access granted via whitelist")
    with tab5:
        st.subheader("Edit Attendance")
        df=storage.get_attendance()
        if df.empty:
            st.info("No records yet")
        else:
            # Ensure 'PhoneNumber' and 'Date' columns are treated as strings for unique()
            df["PhoneNumber"] = df["PhoneNumber"].astype(str)
            df["Date"] = df["Date"].astype(str)

            phone=st.selectbox("Select Phone",df["PhoneNumber"].unique())
            # Filter dates based on selected phone
            available_dates = df[df["PhoneNumber"]==phone]["Date"].unique()
            date_sel=st.selectbox("Select Date", available_dates)

            row=df[(df["PhoneNumber"]==phone)&(df["Date"]==date_sel)].iloc[0]
            st.write("Current:",row.to_dict())
            new_in=st.text_input("IN",row["IN"]); new_out=st.text_input("OUT",row["OUT"])
            # Correctly set default index for selectbox based on current value
            new_wfh=st.selectbox("WFH",["Yes","No"],index=0 if str(row["WFH"]).lower()=="yes" else 1)
            new_leave=st.selectbox("Leave",["Yes","No"],index=0 if str(row["Leave"]).lower()=="yes" else 1)
            reason=st.text_input("Reason for edit")
            if st.button("Save Edit"):
                updates = {}
                for field,newval in {"IN":new_in,"OUT":new_out,"WFH":new_wfh,"Leave":new_leave}.items():
                    old=row[field]
                    if str(newval).strip() != str(old).strip():
                        updates[field] = newval
                        storage.append_edit({
                            "DateTime":datetime.now().isoformat(),"EditedByPhone":st.session_state.user["PhoneNumber"],
                            "EditedByName":st.session_state.user["Name"],"TargetPhone":phone,"Date":date_sel,
                            "Field":field,"OldValue":old,"NewValue":newval,"Reason":reason
                        })
                if updates:
                    storage.update_attendance_fields(phone, date_sel, updates)
                st.success("Updated & logged")
                st.rerun()

    # Keep a bottom Back as well for convenience
    if st.button("Back", key="admin_back_bottom"):
        nav_to("home")

# ROUTER
if st.session_state.page=="login": show_login()
elif st.session_state.page=="signup": show_signup()
elif st.session_state.page=="home": show_home()
elif st.session_state.page=="mark": show_mark()
elif st.session_state.page=="profile": show_profile()
elif st.session_state.page=="admin": show_admin()