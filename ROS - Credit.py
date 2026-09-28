# STREAM 5.8 — EXACT 5.1 TABLE 7 + CLICKABLE POPUP
# STREAM 4.1 — ROW 2 CLEAN + INTERACTIVE
# ============================================================
# RENGY CREDIT ANALYSIS — STREAMLIT / GITHUB VERSION
# ============================================================
# File suggestion: pages/4_Credit_Analysis.py
#
# Uses the same Rengy loan extraction rules, but turns the
# September-only notebook into an all-month interactive dashboard.
# ============================================================

# ============================================================
# RENGY - FINAL RAW LOAN DATA EXTRACTION
# UPDATED VERSION
#
# ADDED:
#   1. Vendor / Channel Partner Name
#   2. Consultant Name
#
# COLUMN POSITION:
#   LEAD ID
#   Vendor/Channel Partner Name
#   Consultant Name
#   Lead Name
#   ...
#
# IMPORTANT RULES:
# - Internal loan _id is the processing key.
# - Visible LOANxxxx is NOT treated as globally unique.
# - Final report does NOT show Loan ID.
# - Unknown numeric statuses are NOT guessed.
# - EMI Amount is blank until exact API field is confirmed.
# - Total Advance EMI is blank until exact API field is confirmed.
# ============================================================


# ============================================================
# 1. IMPORTS
# ============================================================

import re
import hashlib
import html
from urllib.parse import quote
import time
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin
import pandas as pd
import numpy as np
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode, JsCode
import plotly.express as px
import plotly.graph_objects as go

try:
    from streamlit_plotly_events import plotly_events
    T6_CLICK_EVENTS_AVAILABLE = True
    T6_CLICK_EVENTS_ERROR = ""
except Exception as _t6_events_exc:
    plotly_events = None
    T6_CLICK_EVENTS_AVAILABLE = False
    T6_CLICK_EVENTS_ERROR = str(_t6_events_exc)

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# 2. CONFIGURATION
# ============================================================

BASE_URL = "https://apiportal.rengy.in/api"

LOAN_URL = f"{BASE_URL}/loan"
LEAD_URL = f"{BASE_URL}/lead"
USERS_URL = f"{BASE_URL}/users"

# Confirmed-safe loan pagination size.
LOAN_PAGE_LIMIT = 2000

# Keep master pagination conservative as well.
# Smaller master pages prevent Streamlit Cloud / upstream proxy from
# terminating a very large chunked response mid-download.
# Existing skip-pagination still fetches the COMPLETE master dataset.
MASTER_PAGE_LIMIT = 2000

REQUEST_TIMEOUT = 60

# Optional small delay between detail calls.
REQUEST_DELAY = 0.00


# ============================================================
# 3. AUTHENTICATION — RENGY AUTOMATIC LOGIN
# ============================================================
# Streamlit Secrets required:
#
#   RENGY_IDENTIFIER = "your_login_identifier"
#   RENGY_PASSWORD   = "your_login_password"
#
# No manually maintained Bearer/refresh token is required.
# The confirmed Rengy login endpoint and user type are non-secret constants.

def _secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


RENGY_IDENTIFIER = _secret("RENGY_IDENTIFIER")
RENGY_PASSWORD = _secret("RENGY_PASSWORD")

RENGY_LOGIN_URL = "https://apiportal.rengy.in/api/auth/login"
RENGY_USER_TYPE = "rengyStaff"

if not RENGY_IDENTIFIER or not RENGY_PASSWORD:
    st.error(
        "Missing Rengy login credentials in Streamlit Secrets. "
        "Add RENGY_IDENTIFIER and RENGY_PASSWORD."
    )
    st.stop()


_AUTH_LOCK = threading.RLock()
_AUTH_STATE = {
    "access_token": "",
    "refresh_token": "",
}


def _normalize_token(value):
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def rengy_login(force=False):
    """
    Authenticate with the confirmed Rengy login API and keep the returned
    access/refresh tokens only in app memory.

    The password is read only from Streamlit Secrets.
    Tokens are never written to GitHub or a local file.
    """
    with _AUTH_LOCK:
        if _AUTH_STATE["access_token"] and not force:
            return _AUTH_STATE["access_token"]

        response = requests.post(
            RENGY_LOGIN_URL,
            json={
                "identifier": RENGY_IDENTIFIER,
                "password": RENGY_PASSWORD,
                "userType": RENGY_USER_TYPE,
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://portal.rengy.in",
                "Referer": "https://portal.rengy.in/",
            },
            timeout=REQUEST_TIMEOUT,
        )

        try:
            payload = response.json()
        except Exception:
            payload = {}

        if not response.ok:
            message = ""
            if isinstance(payload, dict):
                message = str(
                    payload.get("message")
                    or payload.get("error")
                    or payload.get("detail")
                    or ""
                )[:300]

            raise PermissionError(
                "Rengy automatic login failed. "
                f"HTTP {response.status_code}"
                + (f" — {message}" if message else "")
            )

        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}

        access_token = _normalize_token(data.get("accessToken"))
        refresh_token = _normalize_token(data.get("refreshToken"))

        if not access_token:
            raise PermissionError(
                "Rengy login returned HTTP 200 but data.accessToken was missing."
            )

        _AUTH_STATE["access_token"] = access_token
        _AUTH_STATE["refresh_token"] = refresh_token

        return access_token


def current_auth_headers(force_login=False):
    """
    Build request headers from the current automatically generated tokens.
    """
    token = rengy_login(force=force_login)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://portal.rengy.in",
        "Referer": "https://portal.rengy.in/",
    }

    refresh_token = _AUTH_STATE.get("refresh_token", "")
    if refresh_token:
        headers["x-refresh-token"] = refresh_token

    return headers


# Kept for compatibility with existing dashboard code.
# api_get() itself rebuilds the headers for every request.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://portal.rengy.in",
    "Referer": "https://portal.rengy.in/",
}


# ============================================================
# 4. BASIC HELPERS
# ============================================================

def clean_text(value):
    """
    Convert values safely to clean strings.
    """

    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    value = str(value).strip()

    if value.lower() in {
        "",
        "none",
        "null",
        "nan",
        "na",
        "n/a"
    }:
        return ""

    return value


def safe_dict(value):
    return value if isinstance(value, dict) else {}


def safe_list(value):
    return value if isinstance(value, list) else []


def first_nonblank(*values):
    """
    Return first genuinely populated value.
    """

    for value in values:

        if value is None:
            continue

        if isinstance(value, str):

            value = value.strip()

            if value:
                return value

            continue

        try:
            if pd.isna(value):
                continue
        except Exception:
            pass

        return value

    return ""


def numeric_or_blank(value):
    """
    Convert confirmed numeric fields to number.
    Unknown / invalid -> blank.
    """

    if value is None or value == "":
        return ""

    number = pd.to_numeric(
        value,
        errors="coerce"
    )

    if pd.isna(number):
        return ""

    number = float(number)

    if number.is_integer():
        return int(number)

    return number


def get_nested(data, path):
    """
    Example:
        get_nested(data, "rengyStaffOwnerId.fullName")
    """

    current = data

    for key in path.split("."):

        if not isinstance(current, dict):
            return None

        current = current.get(key)

    return current


def get_value(data, *paths):
    """
    Return first populated value from supplied paths.
    """

    for path in paths:

        value = get_nested(
            data,
            path
        )

        if value not in [
            None,
            "",
            [],
            {}
        ]:
            return value

    return None


# ============================================================
# 5. DATE HELPERS
# ============================================================

def parse_date(value):

    if not value:
        return pd.NaT

    return pd.to_datetime(
        value,
        utc=True,
        errors="coerce"
    )


def format_date(value):

    dt = parse_date(value)

    if pd.isna(dt):
        return ""

    return (
        dt
        .tz_convert("Asia/Kolkata")
        .strftime("%d %b %Y")
    )


def format_datetime(value):

    dt = parse_date(value)

    if pd.isna(dt):
        return ""

    return (
        dt
        .tz_convert("Asia/Kolkata")
        .strftime("%d %b %Y %I:%M %p")
    )


# ============================================================
# 6. API GET — AUTO RE-AUTH + ONE SAFE RETRY
# ============================================================

_HTTP_SESSION = requests.Session()


def api_get(
    url,
    params=None,
    description="API request",
    session=None,
):
    """
    Shared GET helper.

    Keeps the existing authentication behavior, while also handling
    transient transport failures such as:
      - ChunkedEncodingError / IncompleteRead
      - ConnectionError
      - ReadTimeout / ConnectTimeout
      - temporary 429 / 5xx responses

    IMPORTANT:
      - No records are silently skipped.
      - The SAME request is retried.
      - 401/403 still triggers automatic Rengy re-login.
      - A real failure is raised after retries are exhausted.
    """
    client = session if session is not None else _HTTP_SESSION

    max_transport_attempts = 8
    auth_refreshed = False
    last_exc = None

    for attempt_no in range(1, max_transport_attempts + 1):

        try:
            headers = current_auth_headers(force_login=False)

            response = client.get(
                url,
                headers=headers,
                params=params,
                timeout=(15, REQUEST_TIMEOUT),
            )

            # Expired/invalid token -> refresh once, then retry same GET.
            if response.status_code in [401, 403] and not auth_refreshed:
                try:
                    headers = current_auth_headers(force_login=True)
                    auth_refreshed = True
                except Exception as exc:
                    raise PermissionError(
                        "Rengy access token expired and automatic re-login failed. "
                        f"{exc}"
                    ) from exc

                response = client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=(15, REQUEST_TIMEOUT),
                )

            if response.status_code in [401, 403]:
                try:
                    message = response.json()
                    if isinstance(message, dict):
                        message = (
                            message.get("message")
                            or message.get("error")
                            or message.get("detail")
                            or "Unauthorized"
                        )
                    else:
                        message = "Unauthorized"
                except Exception:
                    message = "Unauthorized"

                raise PermissionError(
                    "Rengy API authentication failed even after automatic re-login. "
                    f"HTTP {response.status_code}: {str(message)[:300]}"
                )

            # Retry temporary upstream/server pressure responses.
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt_no < max_transport_attempts:
                    wait_seconds = min(1.5 * (2 ** (attempt_no - 1)), 12.0)
                    print(
                        f"{description} | temporary HTTP {response.status_code} | "
                        f"retry {attempt_no}/{max_transport_attempts} "
                        f"after {wait_seconds:.2f}s"
                    )
                    time.sleep(wait_seconds)
                    continue

            if not response.ok:
                raise RuntimeError(
                    f"\n{description} failed\n"
                    f"URL: {response.url}\n"
                    f"Status: {response.status_code}\n"
                    f"Response: {response.text[:1000]}"
                )

            # Accessing .json() forces the full response body to be consumed.
            # ChunkedEncodingError can therefore occur here as well and is
            # caught by the requests exception handler below.
            try:
                return response.json()
            except requests.exceptions.ChunkedEncodingError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"{description} returned non-JSON data.\n"
                    f"URL: {response.url}\n"
                    f"Response: {response.text[:1000]}"
                ) from exc

        except PermissionError:
            raise

        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectTimeout,
        ) as exc:
            last_exc = exc

            if attempt_no >= max_transport_attempts:
                break

            wait_seconds = min(1.5 * (2 ** (attempt_no - 1)), 12.0)

            print(
                f"{description} | transient connection problem "
                f"({type(exc).__name__}) | "
                f"retry {attempt_no}/{max_transport_attempts} "
                f"after {wait_seconds:.2f}s"
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        f"{description} could not be downloaded after "
        f"{max_transport_attempts} attempts. "
        "The upstream API/proxy closed the response before the complete "
        "payload arrived. No partial data was accepted."
    ) from last_exc


# ============================================================
# 7. FIND RECORD LIST INSIDE RESPONSE
# ============================================================

def find_record_list(
    payload,
    expected_keys=None
):

    expected_keys = expected_keys or [
        "data",
        "results",
        "records",
        "items",
        "loans",
        "leads",
        "users"
    ]

    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    # Check expected keys first.
    for key in expected_keys:

        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):

            result = find_record_list(
                value,
                expected_keys
            )

            if result:
                return result

    # Check nested dictionaries.
    for value in payload.values():

        if isinstance(value, dict):

            result = find_record_list(
                value,
                expected_keys
            )

            if result:
                return result

    return []


# ============================================================
# 8. PROVIDER NORMALIZATION
# ============================================================

def normalize_provider(name):

    name = clean_text(name)

    if not name:
        return ""

    upper = name.upper()

    if "ECOFY" in upper:
        return "ECOFY"

    if "SOLFIN" in upper:
        return "SOLFIN"

    if "FIBE" in upper:
        return "FIBE"

    if "BAJAJ" in upper:
        return "BAJAJ"

    if "NPS" in upper:
        return "NPS"

    if (
        "CREDIT FAIR" in upper
        or "CREDITFAIR" in upper
    ):
        return "CREDIT FAIR"

    if "CASH" in upper:
        return "CASH"

    if "HDFC" in upper:
        return "HDFC"

    if "IDFC" in upper:
        return "IDFC"

    if (
        "SBI" in upper
        or "STATE BANK" in upper
    ):
        return "SBI"

    return name


# ============================================================
# 9. CONFIRMED MAIN STATUS MAPPING
# ============================================================

# Only values we have confirmed.

CONFIRMED_MAIN_STATUS = {
    1: "Documents Submitted",
    4: "Login Done",
    5: "Rejected"
}


def map_confirmed_main_status(value):

    if value is None:
        return ""

    # ------------------------------------------
    # Numeric status
    # ------------------------------------------

    try:

        number = int(
            float(value)
        )

        if number in CONFIRMED_MAIN_STATUS:
            return CONFIRMED_MAIN_STATUS[number]

        # Unknown numeric status.
        return ""

    except Exception:
        pass

    # ------------------------------------------
    # Textual status
    # ------------------------------------------

    text = clean_text(value)

    if not text:
        return ""

    # Do not output unknown numeric text.
    if text.replace(".", "", 1).isdigit():
        return ""

    return text


# ============================================================
# 10. THREE CONFIRMED LOAN POPULATIONS
# ============================================================

LOAN_POPULATIONS = OrderedDict([
    (
        "Loan Request",
        "loanRequest"
    ),
    (
        "Loan Status",
        "loanStatus"
    ),
    (
        "Rejected Loan",
        "rejectedLoan"
    )
])


# ============================================================
# 11. FETCH ONE LOAN POPULATION
# ============================================================

def fetch_loan_population(
    source_name,
    filter_status
):

    print("\n" + "=" * 70)
    print(f"FETCHING: {source_name}")
    print("=" * 70)

    all_records = []

    seen_ids = set()

    previous_page_ids = None

    skip = 0
    page_number = 1

    while True:

        params = {
            "filterStatus": filter_status,
            "limit": LOAN_PAGE_LIMIT,
            "skip": skip
        }

        payload = api_get(
            LOAN_URL,
            params=params,
            description=(
                f"{source_name} "
                f"page {page_number}"
            )
        )

        batch = find_record_list(
            payload,
            expected_keys=[
                "data",
                "loans",
                "results",
                "records",
                "items"
            ]
        )

        print(
            f"{source_name} | "
            f"page={page_number} | "
            f"skip={skip} | "
            f"returned={len(batch)}"
        )

        if not batch:
            break

        current_page_ids = []

        new_records = 0

        for record in batch:

            if not isinstance(record, dict):
                continue

            internal_id = clean_text(
                record.get("_id")
            )

            if internal_id:

                current_page_ids.append(
                    internal_id
                )

                if internal_id in seen_ids:
                    continue

                seen_ids.add(
                    internal_id
                )

            record_copy = dict(record)

            record_copy[
                "_reportSource"
            ] = source_name

            all_records.append(
                record_copy
            )

            new_records += 1

        page_signature = tuple(
            current_page_ids
        )

        if (
            previous_page_ids is not None
            and page_signature
            and page_signature == previous_page_ids
        ):

            print(
                "Repeated API page detected. "
                "Stopping safely."
            )

            break

        previous_page_ids = page_signature

        if new_records == 0:

            print(
                "No new loan IDs found. "
                "Stopping safely."
            )

            break

        # IMPORTANT:
        # Move skip by actual rows returned.
        skip += len(batch)

        page_number += 1

    print(
        f"{source_name}: "
        f"{len(all_records):,} "
        f"unique records fetched"
    )

    return all_records


# ============================================================
# 12. FETCH ALL LOAN POPULATIONS
# ============================================================

def fetch_all_loan_records():

    all_rows = []

    for (
        source_name,
        filter_status
    ) in LOAN_POPULATIONS.items():

        rows = fetch_loan_population(
            source_name,
            filter_status
        )

        all_rows.extend(rows)

    # ------------------------------------------
    # Deduplicate using INTERNAL loan _id.
    # ------------------------------------------

    combined = OrderedDict()

    for record in all_rows:

        internal_id = clean_text(
            record.get("_id")
        )

        if not internal_id:
            continue

        source = clean_text(
            record.get(
                "_reportSource"
            )
        )

        if internal_id not in combined:

            combined[
                internal_id
            ] = dict(record)

            combined[
                internal_id
            ][
                "_reportSources"
            ] = (
                [source]
                if source
                else []
            )

            # Keep the exact row returned by the Loan Status population.
            # This is the same population that powers the CRM Loan Status
            # table where "Disbursed At" is displayed.
            if source == "Loan Status":
                combined[
                    internal_id
                ][
                    "_loanStatusRecord"
                ] = dict(record)

        else:

            if (
                source
                and source not in
                combined[
                    internal_id
                ][
                    "_reportSources"
                ]
            ):

                combined[
                    internal_id
                ][
                    "_reportSources"
                ].append(source)

            # A loan can occur in more than one population. Do not lose
            # the Loan Status row just because Loan Request was seen first.
            if source == "Loan Status":
                combined[
                    internal_id
                ][
                    "_loanStatusRecord"
                ] = dict(record)

    result = list(
        combined.values()
    )

    print("\n" + "=" * 70)

    print(
        "TOTAL UNIQUE INTERNAL LOANS: "
        f"{len(result):,}"
    )

    print("=" * 70)

    return result


# ============================================================
# 13. FETCH CP / VENDOR USER MASTER
# ============================================================

def fetch_user_master(user_type):

    print("\n" + "-" * 70)
    print(f"FETCHING USER MASTER: {user_type}")
    print("-" * 70)

    rows = []

    seen_ids = set()

    previous_page_ids = None

    skip = 0
    page_number = 1

    while True:

        params = {
            "userType": user_type,
            "limit": MASTER_PAGE_LIMIT,
            "skip": skip
        }

        payload = api_get(
            USERS_URL,
            params=params,
            description=(
                f"{user_type} users "
                f"page {page_number}"
            )
        )

        batch = find_record_list(
            payload,
            expected_keys=[
                "users",
                "vendors",
                "channelPartners",
                "data",
                "results",
                "records",
                "items"
            ]
        )

        print(
            f"{user_type} | "
            f"page={page_number} | "
            f"skip={skip} | "
            f"returned={len(batch)}"
        )

        if not batch:
            break

        current_page_ids = []

        new_records = 0

        for user in batch:

            if not isinstance(user, dict):
                continue

            internal_id = clean_text(
                get_value(
                    user,
                    "_id",
                    "id"
                )
            )

            if internal_id:

                current_page_ids.append(
                    internal_id
                )

                if internal_id in seen_ids:
                    continue

                seen_ids.add(
                    internal_id
                )

            rows.append(user)

            new_records += 1

        page_signature = tuple(
            current_page_ids
        )

        if (
            previous_page_ids is not None
            and page_signature
            and page_signature == previous_page_ids
        ):

            print(
                "Repeated user page detected. "
                "Stopping safely."
            )

            break

        previous_page_ids = page_signature

        if new_records == 0:
            break

        skip += len(batch)

        page_number += 1

    print(
        f"{user_type}: "
        f"{len(rows):,} users fetched"
    )

    return rows


# ============================================================
# 14. CREATE CP / VENDOR MASTER
# ============================================================

def create_partner_master(
    records,
    entity_type
):

    master_rows = []

    for user in records:

        if not isinstance(user, dict):
            continue

        internal_id = clean_text(
            get_value(
                user,
                "_id",
                "id"
            )
        )

        user_code = clean_text(
            get_value(
                user,
                "userCode"
            )
        )

        # Main name.
        entity_name = clean_text(
            get_value(
                user,
                "fullName"
            )
        )

        # Confirmed relationship from the supplied
        # CP/vendor extraction structure:
        # rengyStaffOwnerId.fullName
        consultant_name = clean_text(
            get_value(
                user,
                "rengyStaffOwnerId.fullName"
            )
        )

        # Vendor/CP's own region. Never substitute the lead/customer region.
        partner_region = clean_text(
            first_nonblank(
                get_value(user, "region"),
                get_value(user, "region.name"),
                get_value(user, "assignedRegion"),
                get_value(user, "assignedRegion.name"),
                get_value(user, "regionName"),
            )
        )

        master_rows.append({
            "entityType": entity_type,
            "internalId": internal_id,
            "userCode": user_code,
            "entityName": entity_name,
            "consultantName": consultant_name,
            "partnerRegion": partner_region
        })

    return master_rows


# ============================================================
# 15. FETCH CRM LEADS FOR CP/VENDOR RELATIONSHIP
# ============================================================

def fetch_all_crm_leads():

    print("\n" + "-" * 70)
    print("FETCHING CRM LEADS FOR CP/VENDOR MAPPING")
    print("-" * 70)

    rows = []

    seen_ids = set()

    previous_page_ids = None

    skip = 0
    page_number = 1

    while True:

        params = {
            "userType": "rengyStaff",
            "limit": MASTER_PAGE_LIMIT,
            "skip": skip
        }

        payload = api_get(
            f"{BASE_URL}/lead/",
            params=params,
            description=(
                f"CRM leads page "
                f"{page_number}"
            )
        )

        batch = find_record_list(
            payload,
            expected_keys=[
                "data",
                "leads",
                "results",
                "records",
                "items"
            ]
        )

        print(
            f"CRM Leads | "
            f"page={page_number} | "
            f"skip={skip} | "
            f"returned={len(batch)}"
        )

        if not batch:
            break

        current_page_ids = []

        new_records = 0

        for lead in batch:

            if not isinstance(
                lead,
                dict
            ):
                continue

            internal_id = clean_text(
                get_value(
                    lead,
                    "_id",
                    "id"
                )
            )

            if internal_id:

                current_page_ids.append(
                    internal_id
                )

                if internal_id in seen_ids:
                    continue

                seen_ids.add(
                    internal_id
                )

            rows.append(lead)

            new_records += 1

        page_signature = tuple(
            current_page_ids
        )

        if (
            previous_page_ids is not None
            and page_signature
            and page_signature == previous_page_ids
        ):

            print(
                "Repeated CRM lead page detected. "
                "Stopping safely."
            )

            break

        previous_page_ids = page_signature

        if new_records == 0:
            break

        skip += len(batch)

        page_number += 1

    print(
        f"CRM leads fetched: "
        f"{len(rows):,}"
    )

    return rows


# ============================================================
# 16. BUILD PARTNER MASTER INDEXES
# ============================================================

def build_master_indexes(master_rows):

    by_code = {}
    by_internal_id = {}

    for row in master_rows:

        code = clean_text(
            row.get("userCode")
        )

        internal_id = clean_text(
            row.get("internalId")
        )

        if code:

            by_code[
                code.casefold()
            ] = row

        if internal_id:

            by_internal_id[
                internal_id.casefold()
            ] = row

    return (
        by_code,
        by_internal_id
    )


# ============================================================
# 17. BUILD LEAD -> CP/VENDOR/CONSULTANT LOOKUP
# ============================================================

def build_partner_lookup(
    crm_leads,
    cp_master,
    vendor_master
):

    (
        cp_by_code,
        cp_by_internal
    ) = build_master_indexes(
        cp_master
    )

    (
        vendor_by_code,
        vendor_by_internal
    ) = build_master_indexes(
        vendor_master
    )

    by_lead_code = {}
    by_internal_lead_id = {}

    for lead in crm_leads:

        if not isinstance(
            lead,
            dict
        ):
            continue

        # ------------------------------------------
        # Lead identity
        # ------------------------------------------

        lead_code = clean_text(
            get_value(
                lead,
                "leadCode"
            )
        )

        internal_lead_id = clean_text(
            get_value(
                lead,
                "_id",
                "id"
            )
        )

        source_type = clean_text(
            get_value(
                lead,
                "sourceType"
            )
        ).casefold()

        # ------------------------------------------
        # Confirmed relationship fields from
        # the supplied CP/vendor code.
        # ------------------------------------------

        partner_code = clean_text(
            get_value(
                lead,
                "partnerInfo.userCode"
            )
        )

        partner_internal_id = clean_text(
            get_value(
                lead,
                "partnerInfo._id"
            )
        )

        assigned_vendor_id = clean_text(
            get_value(
                lead,
                "assignedVendorId._id",
                "assignedVendorId"
            )
        )

        matched = None

        # ------------------------------------------
        # Vendor
        # ------------------------------------------

        if source_type == "vendor":

            if partner_code:

                matched = vendor_by_code.get(
                    partner_code.casefold()
                )

            if (
                matched is None
                and partner_internal_id
            ):

                matched = (
                    vendor_by_internal.get(
                        partner_internal_id.casefold()
                    )
                )

            if (
                matched is None
                and assigned_vendor_id
            ):

                matched = (
                    vendor_by_internal.get(
                        assigned_vendor_id.casefold()
                    )
                )

        # ------------------------------------------
        # Channel Partner
        # ------------------------------------------

        else:

            if partner_code:

                matched = cp_by_code.get(
                    partner_code.casefold()
                )

            if (
                matched is None
                and partner_internal_id
            ):

                matched = (
                    cp_by_internal.get(
                        partner_internal_id.casefold()
                    )
                )

            if (
                matched is None
                and assigned_vendor_id
            ):

                matched = (
                    cp_by_internal.get(
                        assigned_vendor_id.casefold()
                    )
                )

        # ------------------------------------------
        # Result
        # ------------------------------------------

        if matched is None:

            info = {
                "Vendor/Channel Partner Name": "",
                "Consultant Name": "",
                "Partner Region": ""
            }

        else:

            info = {
                "Vendor/Channel Partner Name":
                    clean_text(
                        matched.get(
                            "entityName"
                        )
                    ),

                "Consultant Name":
                    clean_text(
                        matched.get(
                            "consultantName"
                        )
                    ),

                "Partner Region":
                    clean_text(
                        matched.get(
                            "partnerRegion"
                        )
                    )
            }

        if lead_code:

            by_lead_code[
                lead_code.casefold()
            ] = info

        if internal_lead_id:

            by_internal_lead_id[
                internal_lead_id.casefold()
            ] = info

    return (
        by_lead_code,
        by_internal_lead_id
    )


# ============================================================
# 18. LOAN DETAIL CACHE
# ============================================================

loan_detail_cache = {}


def fetch_loan_detail(
    internal_loan_id
):

    internal_loan_id = clean_text(
        internal_loan_id
    )

    if not internal_loan_id:
        return {}

    if (
        internal_loan_id
        in loan_detail_cache
    ):
        return loan_detail_cache[
            internal_loan_id
        ]

    payload = api_get(
        f"{LOAN_URL}/{internal_loan_id}",
        description=(
            f"Loan detail "
            f"{internal_loan_id}"
        )
    )

    result = {}

    if isinstance(payload, dict):

        data = payload.get("data")

        if isinstance(data, dict):
            result = data
        else:
            result = payload

    loan_detail_cache[
        internal_loan_id
    ] = result

    if REQUEST_DELAY:
        time.sleep(
            REQUEST_DELAY
        )

    return result


# ============================================================
# 19. LEAD DETAIL CACHE
# ============================================================

lead_detail_cache = {}


def fetch_lead_detail(
    internal_lead_id
):

    internal_lead_id = clean_text(
        internal_lead_id
    )

    if not internal_lead_id:
        return {}

    if (
        internal_lead_id
        in lead_detail_cache
    ):
        return lead_detail_cache[
            internal_lead_id
        ]

    payload = api_get(
        f"{LEAD_URL}/{internal_lead_id}",
        description=(
            f"Lead detail "
            f"{internal_lead_id}"
        )
    )

    result = {}

    if isinstance(payload, dict):

        data = payload.get("data")

        if isinstance(data, dict):
            result = data
        else:
            result = payload

    lead_detail_cache[
        internal_lead_id
    ] = result

    if REQUEST_DELAY:
        time.sleep(
            REQUEST_DELAY
        )

    return result


# ============================================================
# 20. AUDIT HELPERS
# ============================================================

def sorted_audit_logs(
    loan_data
):

    logs = safe_list(
        loan_data.get(
            "auditLogs"
        )
    )

    def sort_key(log):

        meta = safe_dict(
            log.get(
                "eventMeta"
            )
        )

        value = first_nonblank(
            meta.get(
                "eventDate"
            ),
            log.get(
                "createdAt"
            )
        )

        dt = parse_date(
            value
        )

        if pd.isna(dt):

            return pd.Timestamp(
                "1900-01-01",
                tz="UTC"
            )

        return dt

    return sorted(
        logs,
        key=sort_key
    )


def audit_event_datetime(
    log
):

    meta = safe_dict(
        log.get(
            "eventMeta"
        )
    )

    return first_nonblank(
        meta.get(
            "eventDate"
        ),
        log.get(
            "createdAt"
        )
    )


def audit_changed_keys(
    log
):

    snapshot = safe_dict(
        log.get(
            "snapShotDoc"
        )
    )

    changed = snapshot.get(
        "changedKeys",
        []
    )

    if not isinstance(
        changed,
        list
    ):
        return []

    return changed


def audit_old_snapshot(
    log
):

    snapshot = safe_dict(
        log.get(
            "snapShotDoc"
        )
    )

    return safe_dict(
        snapshot.get(
            "oldSnapShotDoc"
        )
    )


def audit_new_snapshot(
    log
):

    snapshot = safe_dict(
        log.get(
            "snapShotDoc"
        )
    )

    return safe_dict(
        snapshot.get(
            "newSnapShotDoc"
        )
    )


# ============================================================
# 21. STAGE FROM AUDIT EVENT
# ============================================================

def stage_from_event(
    log
):

    meta = safe_dict(
        log.get(
            "eventMeta"
        )
    )

    new_snapshot = (
        audit_new_snapshot(log)
    )

    candidates = [
        meta.get(
            "loanMainStatusTo"
        ),
        new_snapshot.get(
            "loanMainStatus"
        )
    ]

    for candidate in candidates:

        mapped = (
            map_confirmed_main_status(
                candidate
            )
        )

        if mapped:
            return mapped

    # ------------------------------------------
    # Explicit textual audit descriptions
    # ------------------------------------------

    action = clean_text(
        log.get("action")
    )

    description = clean_text(
        log.get("description")
    )

    combined = (
        f"{action} {description}"
    ).lower()

    if "rejected" in combined:
        return "Rejected"

    if "approved" in combined:
        return "Approved"

    if "login done" in combined:
        return "Login Done"

    if "documents submitted" in combined:
        return "Documents Submitted"

    return ""


# ============================================================
# 22. SUBSTAGE FROM AUDIT EVENT
# ============================================================

def substage_from_event(
    log
):

    changed_keys = (
        audit_changed_keys(log)
    )

    new_snapshot = (
        audit_new_snapshot(log)
    )

    # Explicit textual substatus.
    if "loanSubStatus" in changed_keys:

        value = new_snapshot.get(
            "loanSubStatus"
        )

        if isinstance(
            value,
            str
        ):

            text = clean_text(
                value
            )

            if (
                text
                and not text.replace(
                    ".",
                    "",
                    1
                ).isdigit()
            ):
                return text

    description = clean_text(
        log.get(
            "description"
        )
    ).lower()

    if "disbursed" in description:
        return "Disbursed"

    if "nach done" in description:
        return "NACH Done"

    return ""


# ============================================================
# 23. REJECTION REMARK
# ============================================================

def rejection_remark_from_event(
    log
):

    if stage_from_event(log) != "Rejected":
        return ""

    meta = safe_dict(
        log.get(
            "eventMeta"
        )
    )

    new_snapshot = (
        audit_new_snapshot(log)
    )

    candidates = [
        meta.get(
            "loanRejectedReason"
        ),
        meta.get(
            "rejectionReason"
        ),
        new_snapshot.get(
            "loanRejectedReason"
        ),
        new_snapshot.get(
            "rejectionReason"
        ),
        new_snapshot.get(
            "loanComments"
        )
    ]

    for candidate in candidates:

        text = clean_text(
            candidate
        )

        if text:
            return text

    # Description only for an actual
    # explicit rejection event.
    return clean_text(
        log.get(
            "description"
        )
    )


# ============================================================
# 24. RECONSTRUCT PROVIDER ATTEMPTS
# ============================================================

def reconstruct_attempts(
    loan_data
):

    logs = sorted_audit_logs(
        loan_data
    )

    attempts = []

    current_attempt = None

    for log in logs:

        changed_keys = (
            audit_changed_keys(log)
        )

        old_snapshot = (
            audit_old_snapshot(log)
        )

        new_snapshot = (
            audit_new_snapshot(log)
        )

        # ------------------------------------------
        # New attempt ONLY when provider is
        # explicitly changed/assigned.
        # ------------------------------------------

        if (
            "loanProviderName"
            in changed_keys
        ):

            old_provider = (
                normalize_provider(
                    old_snapshot.get(
                        "loanProviderName"
                    )
                )
            )

            new_provider = (
                normalize_provider(
                    new_snapshot.get(
                        "loanProviderName"
                    )
                )
            )

            if (
                new_provider
                and
                new_provider != old_provider
            ):

                if (
                    current_attempt
                    is not None
                ):
                    attempts.append(
                        current_attempt
                    )

                current_attempt = {
                    "date": format_date(
                        audit_event_datetime(
                            log
                        )
                    ),
                    "provider": new_provider,
                    "stage": "",
                    "substage": "",
                    "remark": ""
                }

        # ------------------------------------------
        # Update only current attempt.
        # ------------------------------------------

        if current_attempt is None:
            continue

        stage = stage_from_event(
            log
        )

        if stage:

            current_attempt[
                "stage"
            ] = stage

        substage = (
            substage_from_event(
                log
            )
        )

        if substage:

            current_attempt[
                "substage"
            ] = substage

        rejection_remark = (
            rejection_remark_from_event(
                log
            )
        )

        if rejection_remark:

            current_attempt[
                "remark"
            ] = rejection_remark

    if (
        current_attempt
        is not None
    ):

        attempts.append(
            current_attempt
        )

    return attempts


# ============================================================
# 25. REQUESTED DATE
# ============================================================

def get_requested_date(
    loan_data
):

    logs = sorted_audit_logs(
        loan_data
    )

    for log in logs:

        meta = safe_dict(
            log.get(
                "eventMeta"
            )
        )

        event_type = clean_text(
            meta.get(
                "eventType"
            )
        ).lower()

        action = clean_text(
            log.get(
                "action"
            )
        ).lower()

        if (
            event_type == "loan_created"
            or action == "loan_created"
            or "loan created" in action
        ):

            return format_date(
                audit_event_datetime(
                    log
                )
            )

    # Existing fallback.
    return format_date(
        loan_data.get(
            "createdAt"
        )
    )


# ============================================================
# 26. LOGIN DONE ON
# ============================================================

def get_login_done_on(
    loan_data
):

    # Confirmed root field.
    root_value = clean_text(
        loan_data.get(
            "loginDoneAt"
        )
    )

    if root_value:

        return format_datetime(
            root_value
        )

    # Confirmed audit eventMeta fallback.
    for log in sorted_audit_logs(
        loan_data
    ):

        meta = safe_dict(
            log.get(
                "eventMeta"
            )
        )

        value = clean_text(
            meta.get(
                "loginDoneAt"
            )
        )

        if value:

            return format_datetime(
                value
            )

    return ""


# ============================================================
# 27. DISBURSED AT
# ============================================================

def get_disbursed_at(
    list_record,
    loan_data
):
    """
    DISBURSED AT — CRM LOAN STATUS DATE FIRST

    Final source priority:

    1. Loan Status population/list row:
         disbursedAt
         firstDisbursedAt

       This is the population behind the CRM Loan Status table where
       "Disbursed At" is displayed.

    2. Individual /loan/{id} detail:
         firstDisbursedAt

    3. Exact audit fallback:
       timestamp of the event where loanSubStatus changes to Disbursed.

    Explicitly NOT used:
         secondDisbursedAt

    If none of the confirmed sources contains a date, return blank.
    """

    # --------------------------------------------------------
    # A. PRIMARY — exact Loan Status population row
    # --------------------------------------------------------
    loan_status_record = safe_dict(
        list_record.get(
            "_loanStatusRecord"
        )
    )

    # If this record itself came from Loan Status, it is also valid
    # even if it predates the _loanStatusRecord preservation change.
    report_sources = list_record.get(
        "_reportSources",
        []
    )

    if not isinstance(report_sources, list):
        report_sources = []

    if (
        not loan_status_record
        and (
            clean_text(
                list_record.get(
                    "_reportSource"
                )
            ) == "Loan Status"
            or "Loan Status" in report_sources
        )
    ):
        loan_status_record = safe_dict(
            list_record
        )

    # The CRM/list payload may expose the display date as either
    # disbursedAt or firstDisbursedAt depending on response shape.
    list_disbursed = clean_text(
        first_nonblank(
            get_value(
                loan_status_record,
                "disbursedAt"
            ),
            get_value(
                loan_status_record,
                "firstDisbursedAt"
            )
        )
    )

    if list_disbursed:
        return format_datetime(
            list_disbursed
        )

    # --------------------------------------------------------
    # B. FALLBACK — individual loan detail first tranche
    # --------------------------------------------------------
    first = clean_text(
        loan_data.get(
            "firstDisbursedAt"
        )
    )

    if first:
        return format_datetime(
            first
        )

    # --------------------------------------------------------
    # C. FINAL FALLBACK — exact substatus -> Disbursed transition
    # --------------------------------------------------------
    for log in sorted_audit_logs(
        loan_data
    ):

        changed_keys = audit_changed_keys(
            log
        )

        if "loanSubStatus" not in changed_keys:
            continue

        new_snapshot = audit_new_snapshot(
            log
        )

        new_substatus = clean_text(
            new_snapshot.get(
                "loanSubStatus"
            )
        )

        # Prefer an explicit textual transition.
        if new_substatus.casefold() == "disbursed":
            event_dt = audit_event_datetime(
                log
            )

            if event_dt:
                return format_datetime(
                    event_dt
                )

        # Some audit rows describe the transition textually even when
        # the snapshot value is not human-readable.
        description = clean_text(
            log.get(
                "description"
            )
        ).casefold()

        if "disbursed" in description:
            event_dt = audit_event_datetime(
                log
            )

            if event_dt:
                return format_datetime(
                    event_dt
                )

    return ""


# ============================================================
# 27B. STATUS EVENT DATE
# ============================================================

def get_status_event_date(loan_data, target_status):
    """
    Returns the latest confirmed audit timestamp for the requested
    main-status transition. Used only for completed outcomes such as
    Approved / Rejected.

    We deliberately do not invent an approval/rejection date from
    unrelated fields.
    """
    target = clean_text(target_status)

    if target not in {"Approved", "Rejected"}:
        return ""

    matched_dt = None

    for log in sorted_audit_logs(loan_data):
        if stage_from_event(log) != target:
            continue

        event_dt = audit_event_datetime(log)

        if event_dt:
            matched_dt = event_dt

    return format_datetime(matched_dt) if matched_dt else ""


# ============================================================
# 28. CURRENT STATUS
# ============================================================

def get_current_status(
    loan_data
):

    root_status = (
        map_confirmed_main_status(
            loan_data.get(
                "loanMainStatus"
            )
        )
    )

    if root_status:
        return root_status

    # Explicit audit fallback.
    for log in reversed(
        sorted_audit_logs(
            loan_data
        )
    ):

        stage = stage_from_event(
            log
        )

        if stage:
            return stage

    return ""


# ============================================================
# 29. CURRENT SUBSTAGE
# ============================================================

def get_current_substage(
    loan_data
):

    root_value = loan_data.get(
        "loanSubStatus"
    )

    # Use root only if textual.
    if isinstance(
        root_value,
        str
    ):

        text = clean_text(
            root_value
        )

        if (
            text
            and not text.replace(
                ".",
                "",
                1
            ).isdigit()
        ):
            return text

    # Explicit textual audit fallback.
    for log in reversed(
        sorted_audit_logs(
            loan_data
        )
    ):

        substage = (
            substage_from_event(
                log
            )
        )

        if substage:
            return substage

    return ""


# ============================================================
# 30. GET CP/VENDOR INFO
# ============================================================

def get_partner_info(
    lead_code,
    internal_lead_id,
    partner_by_lead_code,
    partner_by_internal_lead_id
):

    lead_code = clean_text(
        lead_code
    )

    internal_lead_id = clean_text(
        internal_lead_id
    )

    if lead_code:

        result = (
            partner_by_lead_code.get(
                lead_code.casefold()
            )
        )

        if result:
            return result

    if internal_lead_id:

        result = (
            partner_by_internal_lead_id.get(
                internal_lead_id.casefold()
            )
        )

        if result:
            return result

    return {
        "Vendor/Channel Partner Name": "",
        "Consultant Name": ""
    }


# ============================================================
# 31. BUILD ONE FINAL REPORT ROW
# ============================================================

def build_report_row(
    list_record,
    loan_data,
    partner_by_lead_code,
    partner_by_internal_lead_id
):

    # ------------------------------------------
    # Embedded lead
    # ------------------------------------------

    embedded_lead = safe_dict(
        loan_data.get(
            "leadDetails"
        )
    )

    internal_lead_id = clean_text(
        loan_data.get(
            "leadId"
        )
    )

    # ------------------------------------------
    # Full lead detail
    # ------------------------------------------

    lead_detail = {}

    if internal_lead_id:

        try:

            lead_detail = (
                fetch_lead_detail(
                    internal_lead_id
                )
            )

        except Exception as exc:

            print(
                "  Lead detail failed "
                f"{internal_lead_id}: "
                f"{exc}"
            )

    # ------------------------------------------
    # Lead ID
    # ------------------------------------------

    lead_id = clean_text(
        first_nonblank(
            lead_detail.get(
                "leadCode"
            ),
            embedded_lead.get(
                "leadCode"
            )
        )
    )

    # ------------------------------------------
    # Lead name
    # ------------------------------------------

    lead_name = clean_text(
        first_nonblank(
            lead_detail.get(
                "fullName"
            ),
            embedded_lead.get(
                "fullName"
            )
        )
    )

    # ------------------------------------------
    # Region
    # ------------------------------------------

    region = clean_text(
        first_nonblank(
            lead_detail.get(
                "region"
            ),
            embedded_lead.get(
                "region"
            )
        )
    )

    # ------------------------------------------
    # District
    # ------------------------------------------

    lead_district = clean_text(
        first_nonblank(
            lead_detail.get(
                "district"
            ),
            embedded_lead.get(
                "district"
            )
        )
    )

    # ------------------------------------------
    # Comments
    # ------------------------------------------

    comments = clean_text(
        first_nonblank(
            lead_detail.get(
                "leadComments"
            ),
            embedded_lead.get(
                "leadComments"
            ),
            loan_data.get(
                "loanComments"
            )
        )
    )

    # ------------------------------------------
    # Vendor / CP + Consultant
    # ------------------------------------------

    partner_info = get_partner_info(
        lead_code=lead_id,
        internal_lead_id=internal_lead_id,
        partner_by_lead_code=(
            partner_by_lead_code
        ),
        partner_by_internal_lead_id=(
            partner_by_internal_lead_id
        )
    )

    vendor_cp_name = clean_text(
        partner_info.get(
            "Vendor/Channel Partner Name"
        )
    )

    consultant_name = clean_text(
        partner_info.get(
            "Consultant Name"
        )
    )

    partner_region = clean_text(
        partner_info.get(
            "Partner Region"
        )
    )

    # ------------------------------------------
    # Pricing
    # ------------------------------------------

    pricing = safe_dict(
        loan_data.get(
            "pricing"
        )
    )

    project_value = numeric_or_blank(
        first_nonblank(
            pricing.get(
                "projectValue"
            ),
            loan_data.get(
                "totalProjectValue"
            )
        )
    )

    dynamic_pricing = (
        numeric_or_blank(
            pricing.get(
                "dynamicPriceAmount"
            )
        )
    )

    # ------------------------------------------
    # Current lender
    # ------------------------------------------

    bank_nbfc = (
        normalize_provider(
            loan_data.get(
                "loanProviderName"
            )
        )
    )

    # ------------------------------------------
    # NPS bank information
    # ------------------------------------------

    nps_info = safe_dict(
        loan_data.get(
            "npsBankInfo"
        )
    )

    bank_name = clean_text(
        nps_info.get(
            "npsBankName"
        )
    )

    ifsc_code = clean_text(
        nps_info.get(
            "npsIfscCode"
        )
    )

    branch = clean_text(
        nps_info.get(
            "npsBranch"
        )
    )

    nps_comments = clean_text(
        nps_info.get(
            "npsComments"
        )
    )

    if (
        not comments
        and nps_comments
    ):
        comments = nps_comments

    # ------------------------------------------
    # Loan values
    # ------------------------------------------

    approved_amount = (
        numeric_or_blank(
            loan_data.get(
                "totalApprovedAmount"
            )
        )
    )

    disbursed_amount = (
        numeric_or_blank(
            loan_data.get(
                "totalDisbursedAmount"
            )
        )
    )

    credit_officer = clean_text(
        loan_data.get(
            "creditOfficerName"
        )
    )

    fintech_id = clean_text(
        loan_data.get(
            "fintechId"
        )
    )

    # ------------------------------------------
    # Confirmed financial mappings
    # ------------------------------------------

    subvention_amount = (
        numeric_or_blank(
            loan_data.get(
                "subventionAmount"
            )
        )
    )

    subvention_paid_by = clean_text(
        loan_data.get(
            "subventionAmountHandledBy"
        )
    )

    additional_charges = (
        numeric_or_blank(
            loan_data.get(
                "additionalDocChargesPaid"
            )
        )
    )

    add_charges_paid_by = clean_text(
        loan_data.get(
            "additionalDocChargesHandledBy"
        )
    )

    roi = numeric_or_blank(
        loan_data.get(
            "roiPercent"
        )
    )

    pf = numeric_or_blank(
        loan_data.get(
            "processingFeePercent"
        )
    )

    tenure = numeric_or_blank(
        loan_data.get(
            "tenureMonths"
        )
    )

    advance_emi_count = (
        numeric_or_blank(
            loan_data.get(
                "advanceEmiCount"
            )
        )
    )

    advance_emi_amount = (
        numeric_or_blank(
            loan_data.get(
                "advanceEmiAmount"
            )
        )
    )

    emi_paid_by = clean_text(
        loan_data.get(
            "advanceEmiAmountHandledBy"
        )
    )

    customer_paying_emi = (
        numeric_or_blank(
            loan_data.get(
                "customerPayingEmi"
            )
        )
    )

    loan_pending_amount = (
        numeric_or_blank(
            loan_data.get(
                "loanBalanceAmount"
            )
        )
    )

    # ------------------------------------------
    # NOT CONFIRMED.
    #
    # Do not manufacture these values.
    # ------------------------------------------

    emi_amount = ""
    total_advance_emi = ""

    # ------------------------------------------
    # Dates / statuses
    # ------------------------------------------

    requested_date = (
        get_requested_date(
            loan_data
        )
    )

    login_done_on = (
        get_login_done_on(
            loan_data
        )
    )

    disbursed_at = (
        get_disbursed_at(
            list_record,
            loan_data
        )
    )

    current_status = (
        get_current_status(
            loan_data
        )
    )

    loan_sub_stage = (
        get_current_substage(
            loan_data
        )
    )

    status_event_date = (
        get_status_event_date(
            loan_data,
            current_status
        )
    )

    # ------------------------------------------
    # Source population(s)
    # ------------------------------------------

    sources = list_record.get(
        "_reportSources",
        []
    )

    if not isinstance(
        sources,
        list
    ):
        sources = []

    if not sources:

        source_value = clean_text(
            list_record.get(
                "_reportSource"
            )
        )

        if source_value:
            sources = [
                source_value
            ]

    source = " | ".join(
        [
            clean_text(x)
            for x in sources
            if clean_text(x)
        ]
    )

    # ------------------------------------------
    # Attempts
    # ------------------------------------------

    attempts = reconstruct_attempts(
        loan_data
    )

    login_attempts = len(
        attempts
    )

    # ------------------------------------------
    # Output row
    # ------------------------------------------

    row = OrderedDict()

    row["LEAD ID"] = lead_id

    # NEW COLUMNS
    row[
        "Vendor/Channel Partner Name"
    ] = vendor_cp_name

    row[
        "Consultant Name"
    ] = consultant_name

    row["Lead Name"] = lead_name

    row["Region"] = region
    row["Partner Region"] = partner_region

    row[
        "Requested Date"
    ] = requested_date

    row[
        "Project Value"
    ] = project_value

    row[
        "Bank/NBFC"
    ] = bank_nbfc

    row[
        "Credit Officer"
    ] = credit_officer

    row[
        "Fintech ID"
    ] = fintech_id

    row[
        "Bank Name"
    ] = bank_name

    row[
        "IFSC Code"
    ] = ifsc_code

    row[
        "Branch"
    ] = branch

    row[
        "Comments"
    ] = comments

    row[
        "Dynamic Pricing"
    ] = dynamic_pricing

    row[
        "Approved Amount"
    ] = approved_amount

    row[
        "Lead District"
    ] = lead_district

    row[
        "Disbursed Amount"
    ] = disbursed_amount

    row[
        "Login Done On"
    ] = login_done_on

    row[
        "Disbursed At"
    ] = disbursed_at

    row[
        "Current Status"
    ] = current_status

    row[
        "Status Event Date"
    ] = status_event_date

    row[
        "Loan Sub Stage"
    ] = loan_sub_stage

    row[
        "Subvention Amount"
    ] = subvention_amount

    row[
        "Subvention Paid By"
    ] = subvention_paid_by

    row[
        "Additional Charges"
    ] = additional_charges

    row[
        "Add Charges Paid By"
    ] = add_charges_paid_by

    row["ROI"] = roi

    row["PF"] = pf

    row[
        "Tenure"
    ] = tenure

    row[
        "Advance EMI Count"
    ] = advance_emi_count

    row[
        "Advance EMI Amount"
    ] = advance_emi_amount

    row[
        "EMI Amount"
    ] = emi_amount

    row[
        "EMI Paid By"
    ] = emi_paid_by

    row[
        "Total Advance EMI"
    ] = total_advance_emi

    row[
        "Customer Paying EMI"
    ] = customer_paying_emi

    row[
        "Loan Pending Amount"
    ] = loan_pending_amount

    row[
        "Source"
    ] = source

    row[
        "Login Attempts"
    ] = login_attempts

    # ------------------------------------------
    # Horizontal attempt columns
    # ------------------------------------------

    ordinal_names = {
        1: "1st",
        2: "2nd",
        3: "3rd",
        4: "4th",
        5: "5th"
    }

    # Keep at least five groups.
    output_attempt_count = max(
        5,
        len(attempts)
    )

    for attempt_number in range(
        1,
        output_attempt_count + 1
    ):

        suffix = ordinal_names.get(
            attempt_number,
            f"{attempt_number}th"
        )

        if (
            attempt_number
            <= len(attempts)
        ):

            attempt = attempts[
                attempt_number - 1
            ]

            row[
                f"{suffix} Date"
            ] = clean_text(
                attempt.get(
                    "date"
                )
            )

            row[
                f"{suffix} Provider"
            ] = clean_text(
                attempt.get(
                    "provider"
                )
            )

            row[
                f"{suffix} Stage"
            ] = clean_text(
                attempt.get(
                    "stage"
                )
            )

            row[
                f"{suffix} Substage"
            ] = clean_text(
                attempt.get(
                    "substage"
                )
            )

            row[
                f"{suffix} Remark"
            ] = clean_text(
                attempt.get(
                    "remark"
                )
            )

        else:

            row[
                f"{suffix} Date"
            ] = ""

            row[
                f"{suffix} Provider"
            ] = ""

            row[
                f"{suffix} Stage"
            ] = ""

            row[
                f"{suffix} Substage"
            ] = ""

            row[
                f"{suffix} Remark"
            ] = ""

    return row


# ============================================================


# ============================================================

# ============================================================
# 31A. HIGH-PERFORMANCE DETAIL FETCHING
# ============================================================
#
# IMPORTANT:
# Business logic is unchanged.
#
# Performance changes only:
#   1. 20 concurrent I/O workers.
#   2. One persistent HTTP Session per worker thread.
#   3. urllib3 connection pooling.
#   4. Automatic retry for 429 / 5xx responses.
#   5. Per-loan Streamlit cache for 6 hours.
#   6. Per-lead Streamlit cache for 6 hours.
#   7. Failed concurrent records receive a final sequential recovery.
#
# This means we are reducing NETWORK WAIT, not changing which records
# qualify for any KPI/chart.
# ============================================================

MAX_DETAIL_WORKERS = 20

_thread_local = threading.local()


def _build_worker_session():
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=0.35,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_DETAIL_WORKERS,
        pool_maxsize=MAX_DETAIL_WORKERS * 2,
    )

    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def _get_worker_session():
    session = getattr(_thread_local, "session", None)

    if session is None:
        session = _build_worker_session()
        _thread_local.session = session

    return session


def _extract_payload_dict(payload):
    if not isinstance(payload, dict):
        return {}

    data = payload.get("data")

    if isinstance(data, dict):
        return data

    return payload


@st.cache_data(ttl=21600, show_spinner=False)
def _cached_loan_detail(internal_loan_id, extraction_version="disbursed_list_v2"):
    """
    Cache ONE loan detail independently.

    This is important because rebuilding the master dataframe no longer
    means downloading every unchanged loan detail again.
    """
    internal_loan_id = clean_text(internal_loan_id)

    if not internal_loan_id:
        return {}

    session = _get_worker_session()

    payload = api_get(
        f"{LOAN_URL}/{internal_loan_id}",
        description=f"Loan detail {internal_loan_id}",
        session=session,
    )

    return _extract_payload_dict(payload)


@st.cache_data(ttl=21600, show_spinner=False)
def _cached_lead_detail(internal_lead_id):
    internal_lead_id = clean_text(internal_lead_id)

    if not internal_lead_id:
        return {}

    session = _get_worker_session()

    payload = api_get(
        f"{LEAD_URL}/{internal_lead_id}",
        description=f"Lead detail {internal_lead_id}",
        session=session,
    )

    return _extract_payload_dict(payload)


def fetch_one_loan_detail_fast(internal_loan_id):
    internal_loan_id = clean_text(internal_loan_id)

    if not internal_loan_id:
        return internal_loan_id, {}

    return (
        internal_loan_id,
        _cached_loan_detail(internal_loan_id),
    )


def fetch_one_lead_detail_fast(internal_lead_id):
    internal_lead_id = clean_text(internal_lead_id)

    if not internal_lead_id:
        return internal_lead_id, {}

    return (
        internal_lead_id,
        _cached_lead_detail(internal_lead_id),
    )


def fetch_many_details(
    ids,
    worker,
    label,
    max_workers=MAX_DETAIL_WORKERS,
):
    """
    Fetch unique IDs concurrently.

    Correctness rule:
    A failed request is NEVER silently converted into a valid empty row.
    Failed concurrent requests are retried once more in a conservative
    sequential recovery pass.
    """
    unique_ids = list(
        dict.fromkeys(
            clean_text(x)
            for x in ids
            if clean_text(x)
        )
    )

    if not unique_ids:
        return {}

    results = {}
    failed_ids = []
    total = len(unique_ids)

    progress = st.progress(0, text=label)
    status = st.empty()

    with ThreadPoolExecutor(
        max_workers=min(max_workers, total)
    ) as executor:

        futures = {
            executor.submit(worker, item_id): item_id
            for item_id in unique_ids
        }

        completed = 0

        for future in as_completed(futures):
            item_id = futures[future]

            try:
                returned_id, detail = future.result()

                if isinstance(detail, dict) and detail:
                    results[returned_id] = detail
                else:
                    failed_ids.append(item_id)

            except Exception:
                failed_ids.append(item_id)

            completed += 1

            # Updating Streamlit itself is surprisingly expensive.
            # Repaint only every 100 completions.
            if (
                completed == 1
                or completed % 100 == 0
                or completed == total
            ):
                progress.progress(
                    completed / total,
                    text=label,
                )
                status.caption(
                    f"{completed:,} / {total:,}"
                )

    # Final correctness recovery.
    if failed_ids:
        status.caption(
            f"Recovering {len(failed_ids):,} API records..."
        )

        still_failed = []

        for position, item_id in enumerate(
            failed_ids,
            start=1,
        ):
            try:
                returned_id, detail = worker(item_id)

                if isinstance(detail, dict) and detail:
                    results[returned_id] = detail
                else:
                    still_failed.append(item_id)

            except Exception:
                still_failed.append(item_id)

            if position % 25 == 0:
                time.sleep(0.20)

        if still_failed:
            st.warning(
                f"{len(still_failed):,} API detail records could not be "
                "loaded after retry/recovery. They are excluded rather "
                "than being assigned incorrect dates or outcomes."
            )

    progress.empty()
    status.empty()

    return results


def build_credit_master_optimized(
    loan_list_records,
    partner_by_lead_code,
    partner_by_internal_lead_id,
):
    """
    Optimized master builder.

    Old behavior:
        loan 1 detail -> lead 1 detail -> loan 2 detail -> lead 2 detail...

    New behavior:
        1. fetch all loan details concurrently
        2. discover unique lead IDs
        3. fetch all required lead details concurrently
        4. populate the existing caches
        5. run the SAME build_report_row() business logic locally

    This preserves the original report-row rules while removing the
    sequential network wait from the processing loop.
    """

    loan_ids = [
        clean_text(record.get("_id"))
        for record in loan_list_records
        if clean_text(record.get("_id"))
    ]

    # -------- Phase 1: loan details --------
    all_loan_details = fetch_many_details(
        loan_ids,
        fetch_one_loan_detail_fast,
        "Fetching required loan details...",
    )

    # Populate the existing cache used by build_report_row().
    loan_detail_cache.clear()
    loan_detail_cache.update(all_loan_details)

    # -------- Phase 2: unique lead details --------
    lead_ids = []

    for loan_data in all_loan_details.values():
        if not isinstance(loan_data, dict):
            continue

        internal_lead_id = clean_text(
            loan_data.get("leadId")
        )

        if internal_lead_id:
            lead_ids.append(internal_lead_id)

    # PERFORMANCE FIX:
    # CRM leads were already fetched in bulk before this function.
    # Their records are injected into lead_detail_cache by load_credit_data().
    # Only lead IDs genuinely missing from that bulk master are fetched here.
    missing_lead_ids = [
        lead_id
        for lead_id in dict.fromkeys(lead_ids)
        if lead_id not in lead_detail_cache
    ]

    if missing_lead_ids:
        missing_lead_details = fetch_many_details(
            missing_lead_ids,
            fetch_one_lead_detail_fast,
            "Fetching only missing lead details...",
        )
        lead_detail_cache.update(missing_lead_details)

    # -------- Phase 3: local CPU processing only --------
    report_rows = []
    total = len(loan_list_records)

    progress = st.progress(
        0,
        text="Building credit master database...",
    )

    for index, list_record in enumerate(
        loan_list_records,
        start=1,
    ):
        internal_loan_id = clean_text(
            list_record.get("_id")
        )

        loan_data = all_loan_details.get(
            internal_loan_id,
            {},
        )

        if loan_data:
            try:
                report_rows.append(
                    build_report_row(
                        list_record=list_record,
                        loan_data=loan_data,
                        partner_by_lead_code=partner_by_lead_code,
                        partner_by_internal_lead_id=partner_by_internal_lead_id,
                    )
                )
            except Exception as exc:
                # Do not silently hide rows: this can materially undercount
                # monthly logins/disbursements.
                st.session_state.setdefault(
                    "_credit_row_build_errors",
                    []
                ).append(
                    f"{internal_loan_id}: {type(exc).__name__}"
                )

        if (
            index == 1
            or index % 250 == 0
            or index == total
        ):
            progress.progress(
                min(index / max(total, 1), 1.0),
                text="Building credit master database...",
            )

    progress.empty()

    return pd.DataFrame(report_rows)


# 32. STREAMLIT DATA LOAD
# ============================================================

@st.cache_data(ttl=21600, show_spinner=False)
def load_credit_data(extraction_version="disbursed_list_v2"):
    """
    ONE master load for ALL months.

    Cache duration: 1 hour.
    Month / region / provider / status / consultant filters operate
    only on the returned dataframe and do not call the API again.
    """

    global loan_detail_cache, lead_detail_cache

    loan_detail_cache = {}
    lead_detail_cache = {}

    # --------------------------------------------------------
    # A. Bulk master endpoints
    # --------------------------------------------------------
    cp_users = fetch_user_master("channelPartner")
    vendor_users = fetch_user_master("vendor")

    cp_master = create_partner_master(
        cp_users,
        "Channel Partner",
    )

    vendor_master = create_partner_master(
        vendor_users,
        "Vendor",
    )

    crm_leads = fetch_all_crm_leads()

    # Reuse the already-fetched CRM lead master as the lead-detail cache.
    # This removes hundreds/thousands of duplicate /lead/{id} calls.
    lead_detail_cache.clear()
    for lead in crm_leads:
        if not isinstance(lead, dict):
            continue
        lead_internal_id = clean_text(
            get_value(lead, "_id", "id")
        )
        if lead_internal_id:
            lead_detail_cache[lead_internal_id] = lead

    (
        partner_by_lead_code,
        partner_by_internal_lead_id,
    ) = build_partner_lookup(
        crm_leads=crm_leads,
        cp_master=cp_master,
        vendor_master=vendor_master,
    )

    # Three confirmed loan populations are still fetched and
    # deduplicated using internal loan _id exactly as before.
    loan_list_records = fetch_all_loan_records()

    # --------------------------------------------------------
    # B. Concurrent detail enrichment
    # --------------------------------------------------------
    credit_df = build_credit_master_optimized(
        loan_list_records=loan_list_records,
        partner_by_lead_code=partner_by_lead_code,
        partner_by_internal_lead_id=partner_by_internal_lead_id,
    )

    return credit_df


# ============================================================
# 33. PAGE STYLE — RENGY SOLAR FUTURE / NEON EXECUTIVE
# ============================================================

RENGY_LOGO_B64 = "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"
RENGY_CITY_B64 = "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"

st.markdown(
    """
    <style>
    /* Private/internal dashboard presentation: remove Streamlit chrome and top whitespace. */
    header[data-testid="stHeader"],
    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    [data-testid="stStatusWidget"],
    #MainMenu, footer {
        display:none !important;
        visibility:hidden !important;
        height:0 !important;
    }
    section[data-testid="stMain"] > div { padding-top:0 !important; }
    .main .block-container, .block-container { padding-top:0 !important; margin-top:0 !important; }

    :root {
        --rg-bg:#031326;
        --rg-panel:#061B31;
        --rg-panel2:#082541;
        --rg-line:#15517A;
        --rg-cyan:#14D9FF;
        --rg-mint:#22E3B2;
        --rg-text:#F5FAFF;
        --rg-muted:#88AFD1;
    }

    .block-container {
        max-width: 1840px;
        padding:.24rem .72rem 1.6rem;
    }
    div[data-testid="stVerticalBlock"] { gap:.30rem; }

    /* -------- FILTERS -------- */
    div[data-testid="stSelectbox"],
    div[data-testid="stDateInput"],
    div[data-testid="stTextInput"] {
        margin-bottom:0 !important;
    }
    div[data-testid="stSelectbox"] label,
    div[data-testid="stDateInput"] label,
    div[data-testid="stTextInput"] label {
        color:#DDEEFF !important;
        font-size:9px !important;
        line-height:1 !important;
        font-weight:900 !important;
        margin:0 0 3px 2px !important;
        letter-spacing:.08px !important;
    }
    div[data-baseweb="select"] > div,
    div[data-testid="stDateInput"] > div > div,
    div[data-testid="stTextInput"] > div > div {
        min-height:34px !important;
        height:34px !important;
        border-radius:8px !important;
        border:1px solid #17639B !important;
        background:linear-gradient(180deg,#092844,#061D34) !important;
        box-shadow:
            inset 0 1px 0 rgba(255,255,255,.035),
            0 0 0 1px rgba(0,190,255,.025) !important;
    }
    div[data-baseweb="select"] span,
    div[data-baseweb="select"] input,
    div[data-testid="stDateInput"] input,
    div[data-testid="stTextInput"] input {
        color:#F5FAFF !important;
        font-size:10px !important;
        font-weight:800 !important;
    }
    div[data-testid="stTextInput"] input::placeholder {
        color:#7191AF !important;
    }

    /* Login date range stays bright/white for fast scanning. */
    div[data-testid="stDateInput"] > div > div {
        background:#FFFFFF !important;
        border:1px solid #D0D5DD !important;
        box-shadow:none !important;
    }
    div[data-testid="stDateInput"] input {
        color:#101828 !important;
        background:#FFFFFF !important;
    }
    div[data-testid="stDateInput"] svg {
        fill:#344054 !important;
        color:#344054 !important;
    }

    div[data-testid="stButton"] > button {
        min-height:35px !important;
        height:35px !important;
        border-radius:9px !important;
        border:1px solid #13E5D0 !important;
        background:
            radial-gradient(circle at 20% 20%,rgba(33,255,222,.12),transparent 45%),
            linear-gradient(180deg,#07434D,#062A38) !important;
        color:#5EFFE8 !important;
        font-size:9px !important;
        font-weight:950 !important;
        box-shadow:0 0 13px rgba(0,240,205,.12) !important;
    }
    div[data-testid="stButton"] > button:hover {
        color:#FFFFFF !important;
        border-color:#70FFEC !important;
        box-shadow:0 0 18px rgba(0,240,205,.20) !important;
    }

    /* -------- HERO -------- */
    .rg-hero {
        position:relative;
        height:91px;
        overflow:hidden;
        border:1px solid #125582;
        border-radius:12px;
        background:
            linear-gradient(90deg,
                rgba(2,13,29,1) 0%,
                rgba(3,18,36,.99) 33%,
                rgba(3,18,36,.88) 49%,
                rgba(3,18,36,.18) 72%,
                rgba(3,18,36,.05) 100%),
            url("data:image/webp;base64,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");
        background-size:auto 100%;
        background-position:right center;
        background-repeat:no-repeat;
        box-shadow:
            inset 0 1px 0 rgba(255,255,255,.035),
            0 5px 17px rgba(0,15,34,.16);
    }
    .rg-hero:after {
        content:"";
        position:absolute;
        inset:auto 0 0 0;
        height:2px;
        background:linear-gradient(90deg,#0A83FF,#19E2C3,#04A7D8,transparent);
        opacity:.82;
    }
    .rg-hero-content {
        position:absolute;
        left:15px;
        top:50%;
        transform:translateY(-50%);
        display:flex;
        align-items:center;
        z-index:2;
    }
    .rg-logo {
        width:108px;
        height:52px;
        object-fit:contain;
        filter:drop-shadow(0 0 8px rgba(35,231,190,.12));
    }
    .rg-vline {
        width:1px;
        height:43px;
        margin:0 15px;
        background:linear-gradient(transparent,#16D8E5,transparent);
        opacity:.75;
    }
    .rg-title h1 {
        margin:0 !important;
        padding:0 !important;
        color:#FFFFFF !important;
        font-size:24px !important;
        line-height:1 !important;
        font-weight:950 !important;
        letter-spacing:-.5px;
        text-shadow:0 2px 14px rgba(0,0,0,.32);
    }
    .rg-title h1 span {
        color:#3AE9D3;
    }
    .rg-title p {
        margin:7px 0 0 !important;
        color:#79B9E6 !important;
        font-size:9px !important;
        line-height:1 !important;
        font-weight:850 !important;
        letter-spacing:.12px;
    }
    .rg-live {
        position:absolute;
        right:12px;
        top:9px;
        display:flex;
        align-items:center;
        gap:5px;
        padding:4px 8px;
        border:1px solid rgba(55,244,211,.40);
        border-radius:999px;
        background:rgba(3,28,38,.70);
        color:#6CFFE7;
        font-size:7px;
        font-weight:950;
        backdrop-filter:blur(5px);
    }
    .rg-live i {
        width:6px;height:6px;border-radius:50%;
        background:#42F2AD;
        box-shadow:0 0 8px #42F2AD;
    }

    /* -------- FILTER GLASS BAR -------- */
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stSelectbox"]):not(:has(#table5-clean-header)) {
        background:#FFFFFF !important;
        background-image:none !important;
        border:0 !important;
        border-radius:0 !important;
        padding:6px 0 7px !important;
        box-shadow:none !important;
    }

    /* -------- KPI CARDS -------- */
    .credit-kpis {
        display:grid;
        grid-template-columns:repeat(5,minmax(0,1fr));
        gap:7px;
        margin:5px 0 6px;
        align-items:stretch;
    }
    .ckpi {
        height:92px;
        min-width:0;
        border-radius:12px;
        border:1px solid;
        position:relative;
        overflow:hidden;
        padding:10px 34% 8px 58px;
        box-sizing:border-box;
        box-shadow:
            inset 0 1px 0 rgba(255,255,255,.13),
            0 5px 14px rgba(0,0,0,.16);
    }
    .ckpi:before {
        content:"";
        position:absolute;
        inset:0;
        background:
            radial-gradient(circle at 18% 48%,rgba(255,255,255,.13),transparent 23%),
            linear-gradient(115deg,rgba(255,255,255,.05),transparent 44%);
        pointer-events:none;
    }
    .ckpi-icon {
        position:absolute;
        left:10px;
        top:50%;
        transform:translateY(-50%);
        width:37px;height:37px;
        border-radius:50%;
        display:grid;
        place-items:center;
        background:rgba(255,255,255,.11);
        border:1px solid rgba(255,255,255,.28);
        box-shadow:
            inset 0 0 14px rgba(255,255,255,.09),
            0 0 15px rgba(255,255,255,.10);
        z-index:2;
    }
    .ckpi-icon svg {
        width:22px;height:22px;
        stroke:#FFFFFF;
        stroke-width:2.25;
        fill:none;
        stroke-linecap:round;
        stroke-linejoin:round;
    }
    .ckpi-label {
        position:relative;
        z-index:2;
        color:rgba(255,255,255,.95);
        font-size:11px;
        line-height:1.05;
        font-weight:900;
        letter-spacing:.02px;
        white-space:nowrap;
        overflow:hidden;
        text-overflow:ellipsis;
    }
    .ckpi-main {
        position:relative;
        z-index:2;
        display:flex;
        align-items:baseline;
        gap:4px;
        margin-top:6px;
        min-width:0;
    }
    .ckpi-main b {
        color:#FFF;
        font-size:25px;
        line-height:.95;
        font-weight:950;
        letter-spacing:-.5px;
    }
    .ckpi-main span {
        color:#FFF;
        font-size:10px;
        font-weight:900;
        white-space:nowrap;
    }
    .ckpi-foot {
        position:relative;
        z-index:2;
        margin-top:7px;
        color:rgba(255,255,255,.91);
        font-size:9px;
        line-height:1.05;
        font-weight:800;
        white-space:nowrap;
        overflow:hidden;
        text-overflow:ellipsis;
    }
    .ckpi-foot b {
        color:#FFF;
        font-size:10px;
        font-weight:950;
        margin-left:2px;
    }
    /* KPI money — isolated top-right text, no pill / no background */
    .ckpi-money {
        position:absolute;
        z-index:8;
        top:10px;
        right:10px;
        width:32%;
        max-width:88px;
        text-align:right;
        pointer-events:none;
    }
    .ckpi-money-label {
        display:block;
        margin:0 0 4px 0;
        padding:0;
        background:none !important;
        border:0 !important;
        box-shadow:none !important;
        color:rgba(255,255,255,.78) !important;
        font-size:6.5px;
        line-height:1;
        font-weight:900;
        letter-spacing:.12px;
        text-transform:uppercase;
        white-space:nowrap;
    }
    .ckpi-money-value {
        display:block;
        margin:0;
        padding:0;
        background:none !important;
        border:0 !important;
        box-shadow:none !important;
        color:#FFFFFF !important;
        font-size:13px;
        line-height:1.05;
        font-weight:950;
        white-space:nowrap;
        text-shadow:0 1px 6px rgba(0,0,0,.18);
    }

    .ckpi.total {
        background:linear-gradient(135deg,#03417F,#086CC9 56%,#063C76);
        border-color:#12B9FF;
        box-shadow:0 0 14px rgba(0,156,255,.20),inset 0 1px 0 rgba(255,255,255,.13);
    }
    .ckpi.approved {
        background:linear-gradient(135deg,#007356,#00A878 56%,#05664F);
        border-color:#20EAB2;
        box-shadow:0 0 14px rgba(0,229,169,.18),inset 0 1px 0 rgba(255,255,255,.13);
    }
    .ckpi.pending {
        background:linear-gradient(135deg,#9D5C00,#E79400 56%,#9B5700);
        border-color:#FFC11B;
        box-shadow:0 0 14px rgba(255,169,0,.20),inset 0 1px 0 rgba(255,255,255,.13);
    }
    .ckpi.rejected {
        background:linear-gradient(135deg,#9F1D3D,#E23856 56%,#951A38);
        border-color:#FF5C75;
        box-shadow:0 0 14px rgba(255,48,88,.20),inset 0 1px 0 rgba(255,255,255,.13);
    }
    .ckpi.average {
        background:linear-gradient(135deg,#351789,#6933D7 56%,#3B1B89);
        border-color:#8D5AFF;
        box-shadow:0 0 14px rgba(116,68,255,.20),inset 0 1px 0 rgba(255,255,255,.13);
    }

    @media(max-width:1250px) {
        .rg-hero {height:82px;}
        .rg-logo {width:92px;}
        .rg-title h1 {font-size:21px !important;}
        .rg-title p {font-size:8px !important;}
        .credit-kpis {gap:5px;}
        .ckpi {
            height:88px;
            padding:9px 34% 7px 52px;
        }
        .ckpi-icon {width:33px;height:33px;left:8px;}
        .ckpi-icon svg {width:19px;height:19px;}
        .ckpi-label {font-size:9.5px;}
        .ckpi-main b {font-size:21px;}
        .ckpi-main span {font-size:8.5px;}
        .ckpi-foot {font-size:7.8px;}
        .ckpi-foot b {font-size:8.5px;}
        .ckpi-money {top:9px;right:7px;width:32%;max-width:76px;}
        .ckpi-money-label {font-size:5.8px;}
        .ckpi-money-value {font-size:11px;}
    }
    
    /* Current dashboard phase: clean white background */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    .main,
    .block-container {
        background:#FFFFFF !important;
    }

    /* ======================================================
       ROW 2 — LIGHT EXECUTIVE PANEL
       ====================================================== */
    [data-testid="stHorizontalBlock"]:has(
        [data-testid="stMarkdownContainer"] h3
    ) {
        background:transparent;
    }

    /* Keep selectboxes clean and compact on the light canvas. */
    div[data-baseweb="select"] > div {
        background:#FFFFFF !important;
    }

    /* Table 5 Region filter: no giant dark card. */
    .t5-filter-card {
        background:transparent !important;
        border:0 !important;
        box-shadow:none !important;
        padding:0 !important;
        margin:0 !important;
        min-height:0 !important;
    }

    /* Headings in the analytics area use the same dark ink as Row 1. */
    .block-container h3 {
        color:#252A36 !important;
    }

    /* =========================================================
       ROW 2 FINAL — same clean canvas as Row 1
       ========================================================= */

    /* The Row-2 marker and all Streamlit wrappers containing it. */
    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor),
    div[data-testid="stHorizontalBlock"]:has(#credit-row2-anchor),
    div[data-testid="stVerticalBlockBorderWrapper"]:has(#credit-row2-anchor),
    div[data-testid="stElementContainer"]:has(#credit-row2-anchor) {
        background:#FFFFFF !important;
        background-color:#FFFFFF !important;
        border-color:transparent !important;
        box-shadow:none !important;
    }

    /* Row 2 columns themselves */
    div[data-testid="stHorizontalBlock"]:has(#credit-row2-anchor) > div[data-testid="stColumn"],
    div[data-testid="stHorizontalBlock"]:has(#credit-row2-anchor) > div[data-testid="column"] {
        background:#FFFFFF !important;
        background-color:#FFFFFF !important;
    }

    /* Any bordered Streamlit container inside Row 2 */
    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor)
      div[data-testid="stVerticalBlockBorderWrapper"] {
        background:#FFFFFF !important;
        background-color:#FFFFFF !important;
        border:0 !important;
        box-shadow:none !important;
    }

    /* Row 2 headings */
    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) h3 {
        color:#202634 !important;
        font-weight:800 !important;
        margin-top:0 !important;
        margin-bottom:.12rem !important;
        line-height:1.02 !important;
    }

    /* Stream 2.9: Row 2 gets the same ~half-inch compression used for Row 1. */
    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) [data-testid="stCaptionContainer"] {
        margin-top:3px !important;
        margin-bottom:10px !important;
        line-height:1.30 !important;
    }
    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) [data-testid="stPlotlyChart"] {
        margin-top:8px !important;
        margin-bottom:12px !important;
    }

    /* Compact Table-5 region filter — directly beneath its heading. */
    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor)
      div[data-testid="stSelectbox"] {
        max-width:190px !important;
        margin-left:auto !important;
        margin-top:-4px !important;
        margin-bottom:5px !important;
    }

    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor)
      div[data-testid="stSelectbox"] label {
        color:#23324A !important;
        font-size:11px !important;
        font-weight:800 !important;
        margin-bottom:2px !important;
    }

    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor)
      div[data-baseweb="select"] > div {
        min-height:34px !important;
        height:34px !important;
        background:#FFFFFF !important;
        border:1px solid #D8E2EF !important;
        border-radius:7px !important;
        box-shadow:none !important;
    }

    /* No dark card around Table 5. */
    .t5-filter-card,
    .table5-filter-card {
        background:transparent !important;
        border:0 !important;
        box-shadow:none !important;
        padding:0 !important;
        margin:0 !important;
        min-height:0 !important;
    }

    /* Row 2: Graph 4 + Bank-wise only — clean light canvas */
    div[data-testid="stHorizontalBlock"]:has(#credit-row2-anchor),
    div[data-testid="stHorizontalBlock"]:has(#credit-row2-anchor) > div,
    div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor),
    div[data-testid="stVerticalBlockBorderWrapper"]:has(#credit-row2-anchor) {
        background:#FFFFFF !important;
        background-color:#FFFFFF !important;
        background-image:none !important;
        box-shadow:none !important;
        border-color:transparent !important;
    }
</style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# 34. LOAD
# ============================================================

hero_col, refresh_col = st.columns([9.15, .85], vertical_alignment="center")

with hero_col:
    st.markdown(
        f"""
        <div class="rg-hero">
            <div class="rg-hero-content">
                <img class="rg-logo" src="data:image/png;base64,{RENGY_LOGO_B64}">
                <div class="rg-vline"></div>
                <div class="rg-title">
                    <h1>Credit <span>Analysis</span></h1>
                    <p>End-to-End Loan Journey&nbsp;&nbsp;|&nbsp;&nbsp;From Application to Approval</p>
                </div>
            </div>
            <div class="rg-live"><i></i> LIVE CRM</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with refresh_col:
    if st.button("⟳ Refresh", use_container_width=True, key="credit_refresh_data"):
        load_credit_data.clear()
        _cached_loan_detail.clear()
        _cached_lead_detail.clear()
        st.rerun()

with st.spinner("Updating Credit master data..."):
    df = load_credit_data()

if df.empty:
    st.warning("No credit records were returned by the API.")
    st.stop()


# ============================================================
# 35. CLEAN DASHBOARD FIELDS
# ============================================================

dash = df.copy()

def clean_series(series):
    return series.fillna("").astype(str).str.strip()

def parse_dashboard_datetime(series):
    return pd.to_datetime(series, errors="coerce", dayfirst=True)

def safe_pct(numerator, denominator):
    return (numerator / denominator * 100) if denominator else 0.0

for col in [
    "LEAD ID",
    "Region",
    "Current Status",
    "Loan Sub Stage",
    "Bank/NBFC",
    "Vendor/Channel Partner Name",
    "Consultant Name",
]:
    if col not in dash.columns:
        dash[col] = ""
    dash[col] = clean_series(dash[col])

dash["_login_dt"] = parse_dashboard_datetime(dash["Login Done On"])
dash["_disbursed_dt"] = parse_dashboard_datetime(dash["Disbursed At"])

dash["_is_approved"] = (
    dash["Current Status"].str.casefold().eq("approved")
)
dash["_is_disbursed_substage"] = (
    dash["Loan Sub Stage"].str.casefold().eq("disbursed")
)
dash["_confirmed_disbursed"] = (
    dash["_disbursed_dt"].notna()
    & dash["_is_approved"]
    & dash["_is_disbursed_substage"]
)


# ============================================================
# 36. AVAILABLE MONTHS
# ============================================================

month_values = pd.concat(
    [
        dash["_login_dt"].dropna().dt.to_period("M"),
        dash.loc[dash["_confirmed_disbursed"], "_disbursed_dt"]
            .dropna()
            .dt.to_period("M"),
    ],
    ignore_index=True,
)

available_periods = sorted(month_values.unique(), reverse=True)

period_label_map = {
    p.strftime("%B %Y"): p
    for p in available_periods
}

month_options = ["All Months"] + list(period_label_map.keys())


# ============================================================
# 37. FILTERS — ONE COMPACT ROW
# ============================================================

f1, f2, f3, f4, f5, f6, f7 = st.columns(
    [1.08, .92, 1.08, 1.0, 1.0, 1.08, 1.22],
    gap="small",
)

with f1:
    selected_month = st.selectbox(
        "▣  Reporting Month", month_options, index=0, key="credit_top_month"
    )

with f2:
    region_options = ["All Regions"] + sorted([x for x in dash["Region"].unique() if x])
    selected_region = st.selectbox("⌖  Region", region_options, key="credit_top_region")

with f3:
    bank_options = ["All Banks / NBFCs"] + sorted([x for x in dash["Bank/NBFC"].unique() if x])
    selected_bank = st.selectbox(
        "▤  Bank / NBFC", bank_options, index=0, key="credit_top_bank"
    )

with f4:
    status_options = ["All Statuses"] + sorted(
        [x for x in dash["Current Status"].unique() if x]
    )
    selected_status = st.selectbox(
        "◉  Current Status", status_options, key="credit_top_status"
    )

with f5:
    valid_login_dates = dash["_login_dt"].dropna()
    if valid_login_dates.empty:
        selected_date = None
        st.date_input(
            "◷  Login Date Range", value=None, disabled=True,
            key="credit_top_login_date_disabled",
        )
    else:
        selected_date = st.date_input(
            "◷  Login Date Range",
            value=(),
            min_value=valid_login_dates.min().date(),
            max_value=valid_login_dates.max().date(),
            help="Optional · choose start and end dates based on Login Done On",
            key="credit_top_login_date",
        )

with f6:
    consultant_options = ["All Consultants"] + sorted(
        [x for x in dash["Consultant Name"].unique() if x]
    )
    selected_consultant = st.selectbox(
        "♙  Consultant", consultant_options, key="credit_top_consultant"
    )

with f7:
    search_text = st.text_input(
        "⌕  Search",
        placeholder="Lead / Name / CP / Bank",
        key="credit_top_search",
    ).strip()


# ============================================================
# 38. APPLY NON-DATE FILTERS
# ============================================================

# Signature of the active dashboard selection. Table-5 drill-downs are
# valid only while this exact selection remains active.
_filter_signature_payload = "|".join(
    [
        str(selected_month),
        str(selected_region),
        str(selected_bank),
        str(selected_status),
        str(selected_consultant),
        str(selected_date if selected_date else ""),
        str(search_text),
    ]
)
_dashboard_filter_signature = hashlib.sha1(
    _filter_signature_payload.encode("utf-8")
).hexdigest()[:12]

filtered = dash.copy()

if selected_region != "All Regions":
    filtered = filtered[filtered["Region"].eq(selected_region)]

if selected_bank != "All Banks / NBFCs":
    filtered = filtered[filtered["Bank/NBFC"].eq(selected_bank)]

if selected_status != "All Statuses":
    filtered = filtered[filtered["Current Status"].eq(selected_status)]

if selected_consultant != "All Consultants":
    filtered = filtered[filtered["Consultant Name"].eq(selected_consultant)]

# Optional Login Done On date-range filter. No API refetch is required.
if selected_date:
    if isinstance(selected_date, (tuple, list)):
        if len(selected_date) == 2:
            _login_start, _login_end = selected_date
        elif len(selected_date) == 1:
            _login_start = _login_end = selected_date[0]
        else:
            _login_start = _login_end = None
    else:
        _login_start = _login_end = selected_date

    if _login_start is not None and _login_end is not None:
        _login_day = filtered["_login_dt"].dt.date
        filtered = filtered[
            filtered["_login_dt"].notna()
            & _login_day.ge(_login_start)
            & _login_day.le(_login_end)
        ]

if search_text:
    q = re.escape(search_text)
    search_cols = [
        "LEAD ID",
        "Lead Name",
        "Vendor/Channel Partner Name",
        "Consultant Name",
        "Bank/NBFC",
    ]
    search_mask = pd.Series(False, index=filtered.index)
    for col in search_cols:
        if col in filtered.columns:
            search_mask |= (
                filtered[col]
                .fillna("")
                .astype(str)
                .str.contains(q, case=False, regex=True)
            )
    filtered = filtered[search_mask]


# ============================================================
# 39. PERIOD FLAGS
# ============================================================

# Initialise first so widget reruns can never reach the KPI section
# without a valid label.
report_label = str(selected_month)

if selected_month == "All Months":
    report_label = "All Months"
    login_in_period = filtered["_login_dt"].notna()
    disbursed_in_period = filtered["_confirmed_disbursed"]
    report_start = None
    report_end = None
else:
    selected_period = period_label_map[selected_month]
    report_start = selected_period.to_timestamp()
    report_end = (report_start + pd.offsets.MonthEnd(1)).normalize()

    login_in_period = (
        filtered["_login_dt"].notna()
        & filtered["_login_dt"].dt.to_period("M").eq(selected_period)
    )
    disbursed_in_period = (
        filtered["_confirmed_disbursed"]
        & filtered["_disbursed_dt"].dt.to_period("M").eq(selected_period)
    )

period_login_cases = filtered.loc[login_in_period].copy()
period_disbursed_cases = filtered.loc[disbursed_in_period].copy()

if selected_month == "All Months":
    same_month_mask = (
        filtered["_confirmed_disbursed"]
        & filtered["_login_dt"].notna()
        & (
            filtered["_login_dt"].dt.to_period("M")
            == filtered["_disbursed_dt"].dt.to_period("M")
        )
    )
    older_login_mask = (
        filtered["_confirmed_disbursed"]
        & filtered["_login_dt"].notna()
        & (
            filtered["_login_dt"].dt.to_period("M")
            < filtered["_disbursed_dt"].dt.to_period("M")
        )
    )
else:
    same_month_mask = (
        disbursed_in_period
        & filtered["_login_dt"].notna()
        & filtered["_login_dt"].dt.to_period("M").eq(selected_period)
    )
    older_login_mask = (
        disbursed_in_period
        & filtered["_login_dt"].notna()
        & (filtered["_login_dt"] < report_start)
    )

missing_login_mask = (
    disbursed_in_period
    & filtered["_login_dt"].isna()
)

total_logins = int(login_in_period.sum())
total_disbursed = int(disbursed_in_period.sum())
same_month_disbursed = int(same_month_mask.sum())
older_login_disbursed = int(older_login_mask.sum())
missing_login_disbursed = int(missing_login_mask.sum())
conversion_rate = safe_pct(same_month_disbursed, total_logins)

# Validation: Total Logins is strictly the number of master rows whose
# parsed "Login Done On" falls in the selected reporting period.
# No Current Status / Loan Sub Stage condition is applied to this KPI.
if selected_month != "All Months":
    raw_month_login_count = int(
        (
            dash["_login_dt"].notna()
            & dash["_login_dt"].dt.to_period("M").eq(selected_period)
        ).sum()
    )
else:
    raw_month_login_count = int(dash["_login_dt"].notna().sum())


# ============================================================
# 40. EXECUTIVE KPI STRIP
# ============================================================
# Current Approved / Pending / Rejected = latest CURRENT STATUS.
# Attempt count = actual nonblank 1st–5th lender assignments for
# cases that CURRENTLY belong to that KPI bucket.
# ============================================================

KPI_ATTEMPT_SUFFIXES = ["1st", "2nd", "3rd", "4th", "5th"]

kpi_cases = period_login_cases.copy()

# One current case per valid Lead ID. Blank IDs are not collapsed together.
if "LEAD ID" in kpi_cases.columns:
    _ids = clean_series(kpi_cases["LEAD ID"])
    _with_id = kpi_cases.loc[_ids.ne("")].drop_duplicates("LEAD ID", keep="first")
    _without_id = kpi_cases.loc[_ids.eq("")]
    kpi_cases = pd.concat([_with_id, _without_id], axis=0).sort_index()

_kpi_status = clean_series(kpi_cases["Current Status"]).str.casefold()

approved_mask = _kpi_status.eq("approved")
rejected_mask = _kpi_status.eq("rejected")
pending_mask = ~(approved_mask | rejected_mask)

total_cases_kpi = int(len(kpi_cases))
approved_cases_kpi = int(approved_mask.sum())
pending_cases_kpi = int(pending_mask.sum())
rejected_cases_kpi = int(rejected_mask.sum())

approved_rate_kpi = safe_pct(approved_cases_kpi, total_cases_kpi)
pending_rate_kpi = safe_pct(pending_cases_kpi, total_cases_kpi)
rejected_rate_kpi = safe_pct(rejected_cases_kpi, total_cases_kpi)

# ------------------------------------------------------------
# PROJECT VALUE EXPOSURE — same current-case KPI population
# ------------------------------------------------------------
# Project Value already exists in the Credit master. We use the same
# de-duplicated kpi_cases and the same Approved/Pending/Rejected masks,
# so count KPIs and value KPIs reconcile to the same population.
if "Project Value" in kpi_cases.columns:
    _project_value = pd.to_numeric(
        kpi_cases["Project Value"],
        errors="coerce",
    ).fillna(0).clip(lower=0)
else:
    _project_value = pd.Series(0.0, index=kpi_cases.index)

total_project_value_kpi = float(_project_value.sum())
approved_project_value_kpi = float(_project_value.loc[approved_mask].sum())

# Approved card secondary metric intentionally follows the exact Disbursed
# population shown in its footer (e.g. This Month Disbursed - 128).
# Therefore the displayed Project Value reconciles to those same 128 cases,
# not to the current-status Approved count.
if "Project Value" in period_disbursed_cases.columns:
    disbursed_project_value_kpi = float(
        pd.to_numeric(
            period_disbursed_cases["Project Value"], errors="coerce"
        ).fillna(0).clip(lower=0).sum()
    )
else:
    disbursed_project_value_kpi = 0.0

pending_project_value_kpi = float(_project_value.loc[pending_mask].sum())
rejected_project_value_kpi = float(_project_value.loc[rejected_mask].sum())

approved_value_share_kpi = safe_pct(
    approved_project_value_kpi,
    total_project_value_kpi,
)
pending_value_share_kpi = safe_pct(
    pending_project_value_kpi,
    total_project_value_kpi,
)
rejected_value_share_kpi = safe_pct(
    rejected_project_value_kpi,
    total_project_value_kpi,
)


def _format_money_compact(value):
    value = float(value or 0)
    if abs(value) >= 1e7:
        return f"₹{value / 1e7:.2f} Cr"
    if abs(value) >= 1e5:
        return f"₹{value / 1e5:.2f} L"
    if abs(value) >= 1e3:
        return f"₹{value / 1e3:.1f} K"
    return f"₹{value:,.0f}"


def _recorded_attempt_count(frame):
    if frame.empty:
        return 0
    total = 0
    for suffix in KPI_ATTEMPT_SUFFIXES:
        provider_col = f"{suffix} Provider"
        if provider_col in frame.columns:
            total += int(clean_series(frame[provider_col]).ne("").sum())
    return total


total_attempts_kpi = _recorded_attempt_count(kpi_cases)
approved_attempts_kpi = _recorded_attempt_count(kpi_cases.loc[approved_mask])
pending_attempts_kpi = _recorded_attempt_count(kpi_cases.loc[pending_mask])
rejected_attempts_kpi = _recorded_attempt_count(kpi_cases.loc[rejected_mask])

avg_attempts_kpi = (
    total_attempts_kpi / total_cases_kpi
    if total_cases_kpi else 0.0
)

# Strict reconciliation checks.
assert approved_cases_kpi + pending_cases_kpi + rejected_cases_kpi == total_cases_kpi
assert approved_attempts_kpi + pending_attempts_kpi + rejected_attempts_kpi == total_attempts_kpi


def _mini_bars():
    return """
    <div class="mini-bars" aria-hidden="true">
      <i></i><i></i><i></i><i></i>
      <i></i><i></i><i></i><i></i>
    </div>
    """


KPI_ICONS = {
    "total": """
      <svg viewBox="0 0 24 24">
        <path d="M4 7l8-4 8 4-8 4-8-4z"/>
        <path d="M4 12l8 4 8-4"/>
        <path d="M4 17l8 4 8-4"/>
      </svg>
    """,
    "approved": """
      <svg viewBox="0 0 24 24">
        <path d="M5 12l4 4L19 6"/>
      </svg>
    """,
    "pending": """
      <svg viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="9"/>
        <path d="M12 7v6l4 2"/>
      </svg>
    """,
    "rejected": """
      <svg viewBox="0 0 24 24">
        <path d="M7 7l10 10M17 7L7 17"/>
      </svg>
    """,
    "average": """
      <svg viewBox="0 0 24 24">
        <circle cx="12" cy="5" r="2"/>
        <circle cx="5" cy="18" r="2"/>
        <circle cx="19" cy="18" r="2"/>
        <path d="M12 7v5M12 12H5v4M12 12h7v4"/>
      </svg>
    """,
}


def _card(
    cls,
    label,
    value,
    pct=None,
    attempts=None,
    foot="Attempts",
    project_value=None,
    project_value_label="Project Value",
    project_value_share=None,
):
    """Render one KPI as compact HTML.

    IMPORTANT:
    The HTML is deliberately returned as ONE LINE. This prevents Streamlit's
    Markdown parser from treating indented nested HTML as a code block.
    """
    pct_html = f'<span>({pct:.1f}%)</span>' if pct is not None else ""
    foot_html = (
        f'<div class="ckpi-foot">{foot} - <b>{attempts:,}</b></div>'
        if attempts is not None
        else ""
    )
    money_html = (
        f'<div class="ckpi-money">'
        f'<div class="ckpi-money-label">{project_value_label}</div>'
        f'<div class="ckpi-money-value">{_format_money_compact(project_value)}</div>'
        f'</div>'
        if project_value is not None
        else ""
    )

    return (
        f'<div class="ckpi {cls}">'
        f'<div class="ckpi-icon">{KPI_ICONS[cls]}</div>'
        f'<div class="ckpi-label">{label}</div>'
        f'<div class="ckpi-main"><b>{value:,}</b>{pct_html}</div>'
        f'{foot_html}'
        f'{money_html}'
        f'</div>'
    )


_kpi_cards_html = "".join(
    [
        _card(
            "total",
            "Total Cases",
            total_cases_kpi,
            100.0 if total_cases_kpi else 0.0,
            total_attempts_kpi,
            "Total Attempts",
            total_project_value_kpi,
            "Project Value",
            100.0 if total_project_value_kpi else 0.0,
        ),
        _card(
            "approved",
            "Approved",
            approved_cases_kpi,
            approved_rate_kpi,
            approved_attempts_kpi,
            "Attempts",
            # Keep the Project Value calculation exactly as in 6.4.
            project_value=disbursed_project_value_kpi,
            project_value_label="Project Value",
            project_value_share=approved_value_share_kpi,
        ),
        _card(
            "pending",
            "Pending",
            pending_cases_kpi,
            pending_rate_kpi,
            pending_attempts_kpi,
            project_value=pending_project_value_kpi,
            project_value_label="Project Value",
            project_value_share=pending_value_share_kpi,
        ),
        _card(
            "rejected",
            "Rejected",
            rejected_cases_kpi,
            rejected_rate_kpi,
            rejected_attempts_kpi,
            project_value=rejected_project_value_kpi,
            project_value_label="Project Value",
            project_value_share=rejected_value_share_kpi,
        ),
        (
            f'<div class="ckpi average">'
            f'<div class="ckpi-icon">{KPI_ICONS["average"]}</div>'
            f'<div class="ckpi-label">Avg. Attempts / Case</div>'
            f'<div class="ckpi-main"><b>{avg_attempts_kpi:.2f}</b></div>'
            f'<div class="ckpi-foot">{report_label} - <b>{total_cases_kpi:,} cases</b></div>'
            f'<div class="ckpi-money">'
            f'<div class="ckpi-money-label">Project Value</div>'
            f'<div class="ckpi-money-value">{_format_money_compact(total_project_value_kpi)}</div>'
            f'</div>'
            f'</div>'
        ),
    ]
)

# One uninterrupted HTML block: no Markdown-indented fragments.
st.markdown(
    '<div class="credit-kpis">' + _kpi_cards_html + '</div>',
    unsafe_allow_html=True,
)


# ============================================================
# 41–43. EXECUTIVE ANALYSIS ROW
# Layout:
#   50% = Day-wise Login trend
#   25% = Region-wise Logins
#   25% = Disbursement Cohort
#
# All three visuals use the SAME already-filtered dataframe.
# No API / extraction / business-rule changes are made here.
# ============================================================

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

analysis_1_col, analysis_2_col, analysis_3_col = st.columns(
    [1.80, 1.20, 1.00],
    gap="small",
)


# ------------------------------------------------------------
# 41. ANALYSIS 1 — DAY-WISE LOGIN TREND
# Hover = current status composition for that login day.
# ------------------------------------------------------------
with analysis_1_col:
    st.markdown(f"### 1. Login Volume Trend — {report_label}")
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    _day_source = period_login_cases.copy()

    if _day_source.empty:
        st.info("No login records for the selected filters.")

    elif selected_month == "All Months":
        _day_source["_PlotPeriod"] = _day_source["_login_dt"].dt.to_period("M")
        _day_source["_PlotLabel"] = _day_source["_login_dt"].dt.strftime("%b %Y")
        _period_order = sorted(
            _day_source["_PlotPeriod"].dropna().unique()
        )

        _status_table = (
            _day_source
            .assign(
                _Status=clean_series(_day_source["Current Status"])
                .replace("", "Blank / Unconfirmed")
            )
            .groupby(["_PlotPeriod", "_Status"])
            .size()
            .unstack(fill_value=0)
        )

        _total_table = (
            _day_source
            .groupby("_PlotPeriod")
            .size()
            .reindex(_period_order, fill_value=0)
        )

        _plot_rows = []
        for _period in _period_order:
            _counts = (
                _status_table.loc[_period]
                if _period in _status_table.index
                else pd.Series(dtype="int64")
            )
            _counts = _counts[_counts.gt(0)].sort_values(ascending=False)
            _hover_lines = [
                f"{status} - {int(count):,}"
                for status, count in _counts.items()
            ]
            _plot_rows.append(
                {
                    "X": _period.strftime("%b %Y"),
                    "Logins": int(_total_table.get(_period, 0)),
                    "Hover": "<br>".join(_hover_lines) if _hover_lines else "No status data",
                }
            )

        _login_trend = pd.DataFrame(_plot_rows)

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=_login_trend["X"],
                y=_login_trend["Logins"],
                mode="lines+markers+text",
                text=_login_trend["Logins"],
                textposition="top center",
                customdata=_login_trend[["Hover"]].to_numpy(),
                line=dict(width=3),
                marker=dict(size=9),
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    "Total Logins: <b>%{y}</b><br><br>"
                    "%{customdata[0]}"
                    "<extra></extra>"
                ),
            )
        )

        fig.update_layout(
            height=320,
            margin=dict(l=8, r=8, t=12, b=22),
            xaxis_title="Login Month",
            yaxis_title="Number of Logins",
            showlegend=False,
            hovermode="closest",
        )
        fig.update_yaxes(rangemode="tozero", gridcolor="rgba(120,140,170,.18)")
        fig.update_xaxes(showgrid=False)

        st.plotly_chart(
            fig,
            use_container_width=True,
            config={"displaylogo": False},
        )

    else:
        # Build a complete calendar so zero-login days still appear on the line.
        _calendar = pd.DataFrame(
            {
                "Login_Date": pd.date_range(
                    report_start,
                    report_end,
                    freq="D",
                )
            }
        )

        _day_source["Login_Date"] = _day_source["_login_dt"].dt.normalize()
        _day_source["_Status"] = (
            clean_series(_day_source["Current Status"])
            .replace("", "Blank / Unconfirmed")
        )

        _daily_total = (
            _day_source
            .groupby("Login_Date")
            .size()
            .rename("Logins")
            .reset_index()
        )

        _daily_status = (
            _day_source
            .groupby(["Login_Date", "_Status"])
            .size()
            .unstack(fill_value=0)
        )

        _login_trend = _calendar.merge(
            _daily_total,
            on="Login_Date",
            how="left",
        )
        _login_trend["Logins"] = (
            _login_trend["Logins"]
            .fillna(0)
            .astype(int)
        )
        _login_trend["Day"] = _login_trend["Login_Date"].dt.day

        _hover_text = []
        for _date in _login_trend["Login_Date"]:
            if _date in _daily_status.index:
                _counts = _daily_status.loc[_date]
                _counts = _counts[_counts.gt(0)].sort_values(ascending=False)
                _lines = [
                    f"{status} - {int(count):,}"
                    for status, count in _counts.items()
                ]
                _hover_text.append("<br>".join(_lines))
            else:
                _hover_text.append("No logins")

        _login_trend["Hover"] = _hover_text

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=_login_trend["Day"],
                y=_login_trend["Logins"],
                mode="lines+markers+text",
                text=[
                    str(v) if int(v) > 0 else ""
                    for v in _login_trend["Logins"]
                ],
                textposition="top center",
                customdata=np.column_stack(
                    [
                        _login_trend["Login_Date"].dt.strftime("%d %b %Y"),
                        _login_trend["Hover"],
                    ]
                ),
                line=dict(width=3),
                marker=dict(
                    size=[
                        10 if int(v) > 0 else 5
                        for v in _login_trend["Logins"]
                    ]
                ),
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "Total Logins: <b>%{y}</b><br><br>"
                    "%{customdata[1]}"
                    "<extra></extra>"
                ),
            )
        )

        fig.update_layout(
            height=320,
            margin=dict(l=8, r=8, t=12, b=22),
            xaxis_title=f"Date ({report_start.strftime('%b %Y')})",
            yaxis_title="Number of Logins",
            showlegend=False,
            hovermode="closest",
        )
        fig.update_xaxes(
            dtick=1,
            tickangle=0,
            showgrid=False,
        )
        fig.update_yaxes(
            rangemode="tozero",
            gridcolor="rgba(120,140,170,.18)",
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
            config={"displaylogo": False},
        )


# ------------------------------------------------------------
# 42. ANALYSIS 2 — GEOGRAPHIC / REGION INSIGHTS
# Hover shows status composition + project-value exposure.
# ------------------------------------------------------------
with analysis_2_col:
    st.markdown("### 2. Region-wise Logins")
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    _region_source = period_login_cases.copy()
    _region_source["Region_Display"] = (
        clean_series(_region_source["Region"])
        .replace("", "Blank Region")
    )
    _region_source["_RegionStatus"] = (
        clean_series(_region_source["Current Status"])
        .replace("", "Blank / Unconfirmed")
    )
    _region_source["_RegionStatusCF"] = (
        _region_source["_RegionStatus"].str.casefold()
    )
    _region_source["_RegionProjectValue"] = pd.to_numeric(
        _region_source["Project Value"]
        if "Project Value" in _region_source.columns
        else 0,
        errors="coerce",
    ).fillna(0).clip(lower=0)

    _region_source = _region_source.loc[
        ~_region_source["Region_Display"].str.casefold().isin(
            {"", "blank region", "unknown", "nan", "none"}
        )
    ].copy()

    _region_rows = []
    for _region_name, _rg in _region_source.groupby("Region_Display"):
        _rg_status = _rg["_RegionStatusCF"]
        _approved = int(_rg_status.eq("approved").sum())
        _rejected = int(_rg_status.eq("rejected").sum())
        _pending = int(len(_rg) - _approved - _rejected)

        _approved_value = float(
            _rg.loc[
                _rg_status.eq("approved"),
                "_RegionProjectValue",
            ].sum()
        )
        _total_value = float(_rg["_RegionProjectValue"].sum())

        _status_counts = (
            _rg["_RegionStatus"]
            .value_counts()
        )
        _status_hover = "<br>".join(
            f"{status}: <b>{int(count):,}</b>"
            for status, count in _status_counts.items()
        )

        _region_rows.append(
            {
                "Region_Display": _region_name,
                "Logins": int(len(_rg)),
                "Approved": _approved,
                "Pending": _pending,
                "Rejected": _rejected,
                "ApprovedValue": _approved_value,
                "TotalValue": _total_value,
                "StatusHover": _status_hover,
            }
        )

    _region_logins = pd.DataFrame(_region_rows)

    if _region_logins.empty:
        st.info("No region-wise login records.")
    else:
        _region_logins = _region_logins.sort_values(
            ["Logins", "Region_Display"],
            ascending=[True, False],
        )

        _region_max = max(int(_region_logins["Logins"].max()), 1)
        _region_total = max(int(_region_logins["Logins"].sum()), 1)

        _region_custom = np.column_stack(
            [
                (_region_logins["Logins"] / _region_total * 100).round(1),
                _region_logins["Approved"],
                _region_logins["Pending"],
                _region_logins["Rejected"],
                _region_logins["ApprovedValue"].map(_format_money_compact),
                _region_logins["TotalValue"].map(_format_money_compact),
                _region_logins["StatusHover"],
            ]
        )

        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=[_region_max] * len(_region_logins),
                y=_region_logins["Region_Display"],
                orientation="h",
                marker=dict(color="rgba(65,115,190,.10)"),
                hoverinfo="skip",
                showlegend=False,
            )
        )

        fig.add_trace(
            go.Bar(
                x=_region_logins["Logins"],
                y=_region_logins["Region_Display"],
                orientation="h",
                text=[f"<b>{int(v):,}</b>" for v in _region_logins["Logins"]],
                textposition="inside",
                insidetextanchor="middle",
                textfont=dict(color="#111827", size=12, family="Arial Black"),
                cliponaxis=False,
                customdata=_region_custom,
                hovertemplate=(
                    "<b>%{y}</b><br>"
                    "Login Cases: <b>%{x}</b> (%{customdata[0]}%)<br>"
                    "Approved: <b>%{customdata[1]}</b><br>"
                    "Pending: <b>%{customdata[2]}</b><br>"
                    "Rejected: <b>%{customdata[3]}</b><br>"
                    "Approved Project Value: <b>%{customdata[4]}</b><br>"
                    "Total Project Value: <b>%{customdata[5]}</b>"
                    "<extra></extra>"
                ),
                showlegend=False,
            )
        )

        fig.update_layout(
            barmode="overlay",
            height=320,
            margin=dict(l=8, r=36, t=8, b=10),
            xaxis_title="",
            yaxis_title="",
            bargap=.38,
            hovermode="closest",
        )
        fig.update_xaxes(
            visible=False,
            range=[0, _region_max * 1.18],
            fixedrange=True,
        )
        fig.update_yaxes(
            tickfont=dict(size=10),
            fixedrange=True,
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
            config={
                "displaylogo": False,
                "displayModeBar": False,
            },
        )


def _cohort_fintech_hover(frame):
    """Compact, decision-friendly cohort hover with value reconciliation."""
    if frame.empty:
        return (
            "Project Value: <b>₹0</b><br>"
            "Disbursed Value: <b>₹0</b><br>"
            "Fintech Mix: <b>None</b>"
        )

    _disb = pd.to_numeric(
        frame["Disbursed Amount"] if "Disbursed Amount" in frame.columns else 0,
        errors="coerce",
    ).fillna(0).clip(lower=0)
    _proj = pd.to_numeric(
        frame["Project Value"] if "Project Value" in frame.columns else 0,
        errors="coerce",
    ).fillna(0).clip(lower=0)
    _banks = (
        clean_series(frame["Bank/NBFC"]).replace("", "Unconfirmed")
        if "Bank/NBFC" in frame.columns
        else pd.Series("Unconfirmed", index=frame.index)
    )

    _tmp = pd.DataFrame({
        "Bank": _banks,
        "Project": _proj,
        "Disbursed": _disb,
    })
    _mix = (
        _tmp.groupby("Bank", dropna=False)
        .agg(Cases=("Bank", "size"), Project=("Project", "sum"), Disbursed=("Disbursed", "sum"))
        .sort_values(["Cases", "Disbursed"], ascending=False)
    )
    _fintech_lines = "<br>".join(
        f"{html.escape(str(bank))}: <b>{int(row.Cases):,}</b> · "
        f"PV {_format_money_compact(row.Project)} · "
        f"Disb {_format_money_compact(row.Disbursed)}"
        for bank, row in _mix.iterrows()
    )

    return (
        f"Project Value: <b>{_format_money_compact(float(_proj.sum()))}</b><br>"
        f"Disbursed Value: <b>{_format_money_compact(float(_disb.sum()))}</b><br>"
        f"<br><b>Fintech Mix</b><br>{_fintech_lines}"
    )


# ------------------------------------------------------------
# 43. ANALYSIS 3 — DISBURSEMENT COHORT
# Hover on "Older Login → Disbursed" shows the original login-month
# distribution, e.g. Jun - 3 / Aug - 10.
# ------------------------------------------------------------
with analysis_3_col:
    st.markdown("### 3. Disbursed Cohort")
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    if selected_month == "All Months":
        _cohort_source = filtered.loc[
            filtered["_confirmed_disbursed"]
            & filtered["_disbursed_dt"].notna()
        ].copy()

        if _cohort_source.empty:
            st.info("No confirmed disbursed cases.")
        else:
            _cohort_source["_DisbMonth"] = (
                _cohort_source["_disbursed_dt"].dt.to_period("M")
            )
            _cohort_source["_LoginMonth"] = (
                _cohort_source["_login_dt"].dt.to_period("M")
            )

            _same = int(
                (
                    _cohort_source["_login_dt"].notna()
                    & _cohort_source["_LoginMonth"].eq(
                        _cohort_source["_DisbMonth"]
                    )
                ).sum()
            )
            _older = int(
                (
                    _cohort_source["_login_dt"].notna()
                    & (
                        _cohort_source["_LoginMonth"]
                        < _cohort_source["_DisbMonth"]
                    )
                ).sum()
            )
            _labels = [
                "Same Month",
                "Older Login",
            ]
            _values = [_same, _older]
            _same_rows_all = _cohort_source.loc[
                _cohort_source["_login_dt"].notna()
                & _cohort_source["_LoginMonth"].eq(_cohort_source["_DisbMonth"])
            ].copy()
            _older_rows_all = _cohort_source.loc[
                _cohort_source["_login_dt"].notna()
                & (_cohort_source["_LoginMonth"] < _cohort_source["_DisbMonth"])
            ].copy()
            _hovers = [
                _cohort_fintech_hover(_same_rows_all),
                _cohort_fintech_hover(_older_rows_all),
            ]

            fig = go.Figure(
                go.Bar(
                    x=_labels,
                    y=_values,
                    text=_values,
                    textposition="outside",
                    customdata=np.array(_hovers, dtype=object).reshape(-1, 1),
                    hovertemplate=(
                        "<b>%{x}</b><br>"
                        "Cases: <b>%{y}</b><br><br>"
                        "%{customdata[0]}"
                        "<extra></extra>"
                    ),
                )
            )
            fig.update_layout(
                height=320,
                margin=dict(l=4, r=4, t=12, b=55),
                xaxis_title="",
                yaxis_title="Cases",
                showlegend=False,
            )
            fig.update_xaxes(tickangle=0)
            fig.update_yaxes(rangemode="tozero")

            st.plotly_chart(
                fig,
                use_container_width=True,
                config={"displaylogo": False},
            )

    else:
        _same_month_rows = filtered.loc[same_month_mask].copy()
        _older_rows = filtered.loc[older_login_mask].copy()
        _same_count = int(len(_same_month_rows))
        _older_count = int(len(_older_rows))

        # Month breakdown for the exact older-login cohort.
        if not _older_rows.empty:
            _older_month_counts = (
                _older_rows
                .assign(
                    _LoginMonthLabel=_older_rows["_login_dt"].dt.strftime("%b %Y")
                )
                .groupby("_LoginMonthLabel")
                .size()
                .rename("Cases")
                .reset_index()
            )

            # Sort chronologically using a parsed helper, not alphabetically.
            _older_month_counts["_Sort"] = pd.to_datetime(
                _older_month_counts["_LoginMonthLabel"],
                format="%b %Y",
                errors="coerce",
            )
            _older_month_counts = _older_month_counts.sort_values("_Sort")

            _older_hover = "<br>".join(
                f"{row['_LoginMonthLabel']} - {int(row['Cases']):,}"
                for _, row in _older_month_counts.iterrows()
            )
        else:
            _older_hover = "No older-login cases"

        _same_hover = (
            f"{report_start.strftime('%b %Y')} - {_same_count:,}<br>"
            + _cohort_fintech_hover(_same_month_rows)
        )
        _older_hover = _older_hover + "<br>" + _cohort_fintech_hover(_older_rows)
        _cohort_plot = pd.DataFrame(
            {
                "Cohort": [
                    f"{report_start.strftime('%b')} Login",
                    f"Before {report_start.strftime('%b')}",
                ],
                "Cases": [
                    _same_count,
                    _older_count,
                ],
                "Hover": [
                    _same_hover,
                    _older_hover,
                ],
            }
        )

        fig = go.Figure(
            go.Bar(
                x=_cohort_plot["Cohort"],
                y=_cohort_plot["Cases"],
                text=_cohort_plot["Cases"],
                textposition="outside",
                customdata=_cohort_plot[["Hover"]].to_numpy(),
                hovertemplate=(
                    "<b>%{x} → Disbursed "
                    + report_start.strftime("%b")
                    + "</b><br>"
                    "Cases: <b>%{y}</b><br>"
                    "%{customdata[0]}"
                    "<extra></extra>"
                ),
            )
        )

        fig.update_layout(
            height=320,
            margin=dict(l=4, r=4, t=12, b=55),
            xaxis_title="",
            yaxis_title="Cases",
            showlegend=False,
        )
        fig.update_xaxes(tickangle=0)
        fig.update_yaxes(rangemode="tozero", gridcolor="rgba(120,140,170,.18)")

        st.plotly_chart(
            fig,
            use_container_width=True,
            config={"displaylogo": False},
        )


# ============================================================
st.markdown('<div id="credit-row2-anchor"></div>', unsafe_allow_html=True)
st.markdown("""
<style>
/* STREAM 4.1 — ROW 2 CLEAN INTERACTIVE FINISH */
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) {background:#FFFFFF !important;}
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) h3 {
    color:#111827 !important; font-weight:900 !important; letter-spacing:-.25px !important;
    margin-top:2px !important; margin-bottom:5px !important; line-height:1.08 !important;
}
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) [data-testid="stPlotlyChart"] {
    background:#FFFFFF !important; border:1px solid #EAECF0 !important;
    border-radius:12px !important; box-shadow:0 1px 3px rgba(16,24,40,.045) !important;
    overflow:hidden !important; margin-top:0 !important; margin-bottom:8px !important;
}
/* Give the final Region × Bank matrix a clean report-card finish. */
div[data-testid="stVerticalBlock"]:has(#region-bank-card) [data-testid="stPlotlyChart"] {
    border:1px solid #DDE3EA !important; border-radius:12px !important;
    box-shadow:0 2px 8px rgba(16,24,40,.055) !important; background:#FFFFFF !important;
}
div[data-testid="stVerticalBlock"]:has(#region-bank-card) h3 {
    margin-top:6px !important;
    margin-bottom:7px !important;
    line-height:1.18 !important;
}
div[data-testid="stVerticalBlock"]:has(#region-bank-card) [data-testid="stPlotlyChart"] {
    margin-top:4px !important;
    margin-bottom:10px !important;
}
</style>
""", unsafe_allow_html=True)
# Stream 4.2 — interaction nonces. Closing a Row-2 drill-down rebuilds
# the selectable widget so the old cell/point cannot reopen on later clicks.
if "stage_region_click_nonce" not in st.session_state:
    st.session_state["stage_region_click_nonce"] = 0
if "region_bank_click_nonce" not in st.session_state:
    st.session_state["region_bank_click_nonce"] = 0
if "stage_region_last_handled" not in st.session_state:
    st.session_state["stage_region_last_handled"] = None
if "region_bank_last_handled" not in st.session_state:
    st.session_state["region_bank_last_handled"] = None

# 44–45. SECOND ANALYTICS AREA — BALANCED 2 × 2
#
# LEFT:
#   Top    = Stage × Region
#   Bottom = Rejection Issues → Recovery Bank
#
# RIGHT:
#   Top    = Bank-wise Disbursals
#   Bottom = Region × Bank Acceptance
#
# Stream 2.4 keeps the top pair and bottom pair visually aligned.
# ============================================================

st.markdown("""
<style>
/* Stream 2.7 — zero-waste compact analytics grid */
div[data-testid="stHorizontalBlock"]:has(h3) { row-gap: 8px !important; }

/* Tighten vertical whitespace inside this analytics area. */
#credit-row2-anchor + div { margin-top: -8px !important; }
/* Row 2 charts: never pull charts upward into their headings. */
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) div[data-testid="stPlotlyChart"] {
    margin-top: 6px !important;
    margin-bottom: 10px !important;
}
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) h3 {
    margin-top: 1px !important;
    margin-bottom: 5px !important;
    padding: 0 !important;
    line-height: 1.18 !important;
    min-height: 27px !important;
}

/* Dense executive layout: no dark chart shells, compact headings/captions. */
#credit-row2-anchor ~ div h3 { margin-top: 3px !important; margin-bottom: 6px !important; padding-bottom:0 !important; color:#050B18 !important; font-weight:900 !important; line-height:1.14 !important; min-height:29px !important; }
#credit-row2-anchor ~ div [data-testid="stCaptionContainer"] { display:none !important; height:0 !important; margin:0 !important; padding:0 !important; }
#credit-row2-anchor ~ div iframe { background:#FFFFFF !important; }
#credit-row2-anchor ~ div [data-testid="stElementContainer"] { margin-top:0 !important; margin-bottom:0 !important; }

/* Keep dataframe chrome calm/light where Streamlit allows theme overrides. */
div[data-testid="stDataFrame"] {
    border-radius: 10px;
    overflow: hidden;
}
</style>
""", unsafe_allow_html=True)

_second_left, _second_right = st.columns(
    [1, 1],
    gap="small",
)

with _second_left:
    # --------------------------------------------------------
    # 44A. TABLE 4 — CASES BY STAGE & REGION
    # --------------------------------------------------------
    # ------------------------------------------------------------
    # 4. STAGE × REGION — COMPACT CLICKABLE PIVOT
    # Cell format: COUNT | AVG TAT
    #
    # BUSINESS TAT:
    # Approved:
    #   Login Done -> Disbursed At when disbursed.
    #   If approved but not yet disbursed, Login Done -> today.
    #
    # Documents Submitted / Login Done / Rejected:
    #   Login Done -> today.
    # ------------------------------------------------------------
    _g4 = period_login_cases.copy()

    for _col in ["Region", "Current Status", "Login Done On", "Disbursed At"]:
        if _col not in _g4.columns:
            _g4[_col] = ""

    _g4["Region_Display"] = clean_series(_g4["Region"])
    _g4["Stage"] = clean_series(_g4["Current Status"]).replace(
        "", "Blank / Unconfirmed"
    )
    _g4["_login_dt_g4"] = parse_dashboard_datetime(_g4["Login Done On"])
    _g4["_disbursed_dt_g4"] = parse_dashboard_datetime(_g4["Disbursed At"])

    _g4 = _g4.loc[
        ~_g4["Region_Display"].str.casefold().isin(
            {"", "unknown", "nan", "none", "blank", "blank region"}
        )
    ].copy()

    _today_g4 = pd.Timestamp.now().normalize()
    _g4_stage_order = [
        "Approved",
        "Documents Submitted",
        "Login Done",
        "Rejected",
    ]
    _g4 = _g4.loc[_g4["Stage"].isin(_g4_stage_order)].copy()

    # Active / rejected ageing continues through today.
    _g4["_tat_end_g4"] = _today_g4

    # Approved becomes successful at actual disbursement.
    _approved_with_disb_g4 = (
        _g4["Stage"].eq("Approved")
        & _g4["_disbursed_dt_g4"].notna()
    )
    _g4.loc[
        _approved_with_disb_g4, "_tat_end_g4"
    ] = _g4.loc[
        _approved_with_disb_g4, "_disbursed_dt_g4"
    ]

    _g4["TAT Days"] = (
        _g4["_tat_end_g4"] - _g4["_login_dt_g4"]
    ).dt.total_seconds() / 86400.0

    _g4.loc[
        _g4["_login_dt_g4"].isna() | (_g4["TAT Days"] < 0),
        "TAT Days",
    ] = pd.NA

    st.markdown("### 4. Credit Stage Performance by Region")
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    if _g4.empty:
        st.info("No Region × Stage records for the selected filters.")
    else:
        _g4_summary = (
            _g4.groupby(["Region_Display", "Stage"], dropna=False)
            .agg(
                Count=("LEAD ID", "size"),
                Avg_TAT=("TAT Days", "mean"),
            )
            .reset_index()
        )

        _preferred_regions_g4 = [
            "Secunderabad",
            "Warangal",
            "Nizamabad",
            "Rayalaseema",
            "Vijayawada",
            "Nellore",
            "Vizag",
        ]

        _available_regions_g4 = (
            _g4_summary["Region_Display"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )

        _region_order_g4 = [
            _r for _r in _preferred_regions_g4
            if _r in _available_regions_g4
        ] + sorted(
            [
                _r for _r in _available_regions_g4
                if _r not in _preferred_regions_g4
            ],
            key=lambda _x: _x.casefold(),
        )

        _display_g4 = pd.DataFrame({"Region": _region_order_g4})

        for _stage in _g4_stage_order:
            _lookup_g4 = (
                _g4_summary.loc[_g4_summary["Stage"].eq(_stage)]
                .set_index("Region_Display")
            )
            _values_g4 = []

            for _region in _region_order_g4:
                if _region not in _lookup_g4.index:
                    _values_g4.append("—")
                    continue

                _count_g4 = int(_lookup_g4.loc[_region, "Count"])
                _avg_g4 = _lookup_g4.loc[_region, "Avg_TAT"]

                _values_g4.append(
                    f"{_count_g4} | —"
                    if pd.isna(_avg_g4)
                    else f"{_count_g4} | {float(_avg_g4):.0f}d"
                )

            _display_g4[_stage] = _values_g4

        # Center every visible value, including Region.
        _styled_g4 = (
            _display_g4.style
            .set_properties(
                **{
                    "text-align": "center",
                    "vertical-align": "middle",
                    "font-weight": "800",
                    "color": "#08213D",
                    "border-color": "#FFFFFF",
                }
            )
            .set_properties(
                subset=["Region"],
                **{"background-color": "#F8FAFC"}
            )
            .set_properties(
                subset=["Approved"],
                **{"background-color": "#DCFCE7"}
            )
            .set_properties(
                subset=["Documents Submitted"],
                **{"background-color": "#FEF3C7"}
            )
            .set_properties(
                subset=["Login Done"],
                **{"background-color": "#DBEAFE"}
            )
            .set_properties(
                subset=["Rejected"],
                **{"background-color": "#FEE2E2"}
            )
            .set_table_styles(
                [
                    {
                        "selector": "th",
                        "props": [
                            ("text-align", "center"),
                            ("vertical-align", "middle"),
                            ("font-weight", "800"),
                        ],
                    }
                ]
            )
        )

        # Exact compact height: header + visible rows, with almost no empty footer.
        _g4_table_height = 38 + (len(_display_g4) * 35) + 4

        _g4_event = st.dataframe(
            _styled_g4,
            use_container_width=True,
            hide_index=True,
            height=_g4_table_height,
            column_config={
                "Region": st.column_config.TextColumn("Region", width="medium"),
                "Approved": st.column_config.TextColumn(
                    "Approved",
                    help="Count | Avg TAT · Login Done → Disbursed At; until then → today",
                    width="small",
                ),
                "Documents Submitted": st.column_config.TextColumn(
                    "Documents Submitted",
                    help="Count | Avg ageing · Login Done → today",
                    width="small",
                ),
                "Login Done": st.column_config.TextColumn(
                    "Login Done",
                    help="Count | Avg ageing · Login Done → today",
                    width="small",
                ),
                "Rejected": st.column_config.TextColumn(
                    "Rejected",
                    help="Count | Avg ageing · Login Done → today",
                    width="small",
                ),
            },
            on_select="rerun",
            selection_mode="single-cell",
            key=(
                "stage_region_tat_matrix_"
                f"{_dashboard_filter_signature}_"
                f"{st.session_state['stage_region_click_nonce']}"
            ),
        )

        _selected_cells_g4 = []
        try:
            _selected_cells_g4 = list(_g4_event.selection.cells)
        except Exception:
            try:
                _selected_cells_g4 = list(
                    _g4_event.get("selection", {}).get("cells", [])
                )
            except Exception:
                _selected_cells_g4 = []

        if _selected_cells_g4:
            _selected_g4 = _selected_cells_g4[0]
            _g4_selection_signature = repr(_selected_g4)
            if st.session_state.get("stage_region_last_handled") == _g4_selection_signature:
                _selected_g4 = None
            else:
                st.session_state["stage_region_last_handled"] = _g4_selection_signature

        if _selected_cells_g4 and _selected_g4 is not None:
            _selected_row_g4 = None
            _selected_col_g4 = None

            if isinstance(_selected_g4, (list, tuple)) and len(_selected_g4) >= 2:
                _selected_row_g4 = _selected_g4[0]
                _selected_col_g4 = _selected_g4[1]
            elif isinstance(_selected_g4, dict):
                _selected_row_g4 = (
                    _selected_g4.get("row")
                    if "row" in _selected_g4
                    else _selected_g4.get("row_index")
                )
                _selected_col_g4 = (
                    _selected_g4.get("column")
                    if "column" in _selected_g4
                    else _selected_g4.get("column_name")
                )

            if isinstance(_selected_col_g4, int):
                _cols_g4 = _display_g4.columns.tolist()
                if 0 <= _selected_col_g4 < len(_cols_g4):
                    _selected_col_g4 = _cols_g4[_selected_col_g4]

            try:
                _selected_row_g4 = int(_selected_row_g4)
            except Exception:
                _selected_row_g4 = None

            if (
                _selected_row_g4 is not None
                and 0 <= _selected_row_g4 < len(_display_g4)
                and _selected_col_g4 in _g4_stage_order
            ):
                _selected_region_g4 = str(
                    _display_g4.iloc[_selected_row_g4]["Region"]
                )
                _selected_stage_g4 = str(_selected_col_g4)

                # IMPORTANT: consume the click immediately.  The dialog can open
                # in this run, but the next rerun (including the native X close)
                # receives a fresh dataframe widget key.  This clears Streamlit's
                # red selected-cell box and prevents the old popup reopening when
                # the user clicks another filter/tab/widget.
                st.session_state["stage_region_click_nonce"] += 1
                st.session_state["stage_region_last_handled"] = None

                _detail_g4 = _g4.loc[
                    _g4["Region_Display"].eq(_selected_region_g4)
                    & _g4["Stage"].eq(_selected_stage_g4)
                ].copy()

                if not _detail_g4.empty:
                    _detail_g4["TAT Days"] = (
                        pd.to_numeric(_detail_g4["TAT Days"], errors="coerce")
                        .round(0)
                        .astype("Int64")
                    )

                    _detail_candidates_g4 = [
                        "LEAD ID",
                        "Lead Code",
                        "Customer Name",
                        "Mobile",
                        "Region",
                        "Bank/NBFC",
                        "Loan Provider",
                        "Consultant Name",
                        "Current Status",
                        "Loan Sub Stage",
                        "Login Done On",
                        "Disbursed At",
                        "TAT Days",
                        "Project Value",
                        "Disbursed Amount",
                        "Loan Amount",
                        "Attempt Priority",
                    ]
                    _detail_cols_g4 = [
                        _c for _c in _detail_candidates_g4
                        if _c in _detail_g4.columns
                    ]

                    if not _detail_cols_g4:
                        _detail_cols_g4 = [
                            _c for _c in _detail_g4.columns
                            if not str(_c).startswith("_")
                        ][:12]

                    _detail_show_g4 = (
                        _detail_g4[_detail_cols_g4]
                        .reset_index(drop=True)
                    )

                    @st.dialog(
                        f"{_selected_region_g4} × {_selected_stage_g4}",
                        width="large",
                    )
                    def _show_stage_region_details():
                        st.markdown(
                            f"<div style='text-align:center;font-size:0.88rem;"
                            f"font-weight:700;color:#475467;margin-top:-8px;"
                            f"margin-bottom:8px;'>{len(_detail_show_g4):,} cases</div>",
                            unsafe_allow_html=True,
                        )

                        _popup_style_g4 = (
                            _detail_show_g4.style
                            .set_properties(
                                **{
                                    "text-align": "center",
                                    "vertical-align": "middle",
                                }
                            )
                            .set_table_styles(
                                [
                                    {
                                        "selector": "th",
                                        "props": [
                                            ("text-align", "center"),
                                            ("vertical-align", "middle"),
                                        ],
                                    }
                                ]
                            )
                        )

                        st.dataframe(
                            _popup_style_g4,
                            use_container_width=True,
                            hide_index=True,
                            height=min(
                                430,
                                40 + (len(_detail_show_g4) * 35),
                            ),
                        )

                        _download_name_g4 = (
                            f"stage_region_{_selected_region_g4}_"
                            f"{_selected_stage_g4}.csv"
                            .replace(" ", "_")
                            .replace("/", "-")
                        )

                        st.download_button(
                            "Download cases",
                            data=_detail_show_g4.to_csv(index=False).encode(
                                "utf-8-sig"
                            ),
                            file_name=_download_name_g4,
                            mime="text/csv",
                            use_container_width=True,
                            key=(
                                "download_stage_region_"
                                f"{_selected_row_g4}_"
                                f"{_selected_stage_g4}"
                            ),
                        )

                    _show_stage_region_details()


    # --------------------------------------------------------
    # 44A-2. REJECTION STORY -> LENDER RECOVERY
    # IMPORTANT: rejection reason comes from the REJECTED ATTEMPT'S OWN
    # <Nth> Remark field — never from the case-level/final Comments field.
    # This answers the operational story:
    #   "Low CIBIL at ECOFY -> moved to NPS -> approved"
    # and also exposes cases that were never moved after rejection.
    # --------------------------------------------------------
    st.markdown("<div style='height:0;margin-top:-10px'></div>", unsafe_allow_html=True)
    st.markdown("### 6. Rejection Story → Lender Recovery")
    st.caption("Why a lender rejected the case, where it moved next, what recovered, and what was left untouched.")

    _ri_source = period_login_cases.copy()
    _ri_ordinals = {1:"1st", 2:"2nd", 3:"3rd", 4:"4th", 5:"5th", 6:"6th", 7:"7th"}

    def _ri_reason_bucket(value):
        """Compact operational reason from the rejected attempt's own remark."""
        raw = re.sub(r"\s+", " ", clean_text(value)).strip()
        t = raw.casefold()
        if not t:
            return "Reason not recorded"
        if "cibil" in t and any(x in t for x in ["low", "score", "below"]):
            return "Low CIBIL"
        if any(x in t for x in ["dpd", "overdue", "write off", "write-off", "wof", "default"]):
            return "DPD / Overdue / Default"
        if any(x in t for x in ["abb", "banking", "bank statement"]):
            return "Low ABB / Banking"
        if any(x in t for x in ["income", "salary", "itr"]):
            return "Income Issue"
        if any(x in t for x in ["document", "docs", "kyc", "statement pending"]):
            return "Documentation"
        if any(x in t for x in ["duplicate", "same applicant", "same customer"]):
            return "Duplicate / Existing"
        if any(x in t for x in ["policy", "eligibility", "age"]):
            return "Policy / Eligibility"
        if any(x in t for x in ["emi", "roi", "obligation"]):
            return "EMI / Eligibility"
        # Preserve a real source reason instead of hiding it under "Other".
        short = raw.split("|")[0].strip(" -–—:;")
        return (short[:42] + "…") if len(short) > 43 else short

    _ri_rows = []
    for _idx, _row in _ri_source.iterrows():
        _attempts = []
        for _a_no, _suffix in _ri_ordinals.items():
            _provider = normalize_provider(clean_text(_row.get(f"{_suffix} Provider", "")))
            if not _provider:
                continue
            _stage = clean_text(_row.get(f"{_suffix} Stage", ""))
            _substage = clean_text(_row.get(f"{_suffix} Substage", ""))
            _remark = clean_text(_row.get(f"{_suffix} Remark", ""))
            _attempts.append({
                "Attempt": _a_no,
                "Provider": _provider,
                "Outcome": classify_attempt_outcome(_stage, _substage),
                "Remark": _remark,
            })

        if not _attempts:
            continue

        for _pos, _att in enumerate(_attempts):
            if _att["Outcome"] != "Rejected":
                continue

            _later = [x for x in _attempts[_pos + 1:] if x["Provider"]]
            _approved_later = next((x for x in _later if x["Outcome"] == "Approved"), None)
            _next = _later[0] if _later else None

            if _approved_later:
                _journey_status = "Recovered"
                _destination = _approved_later["Provider"]
            elif _next:
                _journey_status = "Pending / In Progress"
                _destination = _next["Provider"]
            else:
                _journey_status = "Untouched"
                _destination = "Not moved"

            _ri_rows.append({
                "_CaseIndex": _idx,
                "Reason": _ri_reason_bucket(_att["Remark"]),
                "Rejected Bank": _att["Provider"],
                "Rejected Attempt": _att["Attempt"],
                "Journey Status": _journey_status,
                "Destination Bank": _destination,
            })

    if _ri_rows:
        _ri_df = pd.DataFrame(_ri_rows)

        # Each source rejection event appears once. The headline story is ranked
        # by rejected cases so the most important operational leakage is first.
        _reason_counts = (
            _ri_df.groupby("Reason")
            .agg(
                Rejected=("_CaseIndex", "nunique"),
                Recovered=("Journey Status", lambda x: int((x == "Recovered").sum())),
                Pending=("Journey Status", lambda x: int((x == "Pending / In Progress").sum())),
                Untouched=("Journey Status", lambda x: int((x == "Untouched").sum())),
            )
            .sort_values(["Rejected", "Recovered"], ascending=False)
        )

        _ri_html = []
        for _reason, _stats in _reason_counts.head(6).iterrows():
            _reason_slice = _ri_df.loc[_ri_df["Reason"].eq(_reason)].copy()
            _recovered = _reason_slice.loc[_reason_slice["Journey Status"].eq("Recovered")]

            if not _recovered.empty:
                _paths = (
                    _recovered.groupby(["Rejected Bank", "Destination Bank"])["_CaseIndex"]
                    .nunique().sort_values(ascending=False)
                )
                (_from_bank, _to_bank), _path_n = _paths.index[0], int(_paths.iloc[0])
                _path_txt = f"<b>{html.escape(str(_from_bank))}</b> → <b>{html.escape(str(_to_bank))}</b> → Approved <b>{_path_n}</b>"
            else:
                _rej_banks = _reason_slice.groupby("Rejected Bank")["_CaseIndex"].nunique().sort_values(ascending=False)
                _from_bank = str(_rej_banks.index[0]) if not _rej_banks.empty else "—"
                _path_txt = f"<b>{html.escape(_from_bank)}</b> → No later approval"

            _rej = int(_stats["Rejected"])
            _rec = int(_stats["Recovered"])
            _pend = int(_stats["Pending"])
            _unt = int(_stats["Untouched"])
            _rate = (_rec / _rej * 100.0) if _rej else 0.0

            _ri_html.append(
                "<div class='ri-story-row'>"
                f"<div class='ri-reason'><b>{html.escape(str(_reason))}</b><span>{_rej:,} rejected cases</span></div>"
                f"<div class='ri-path'>{_path_txt}<span>Recovery {_rec:,}/{_rej:,} ({_rate:.0f}%)</span></div>"
                f"<div class='ri-state'><b>{_pend:,}</b><span>Pending</span></div>"
                f"<div class='ri-state ri-untouched'><b>{_unt:,}</b><span>Untouched</span></div>"
                "</div>"
            )

        _total_rej_story = int(_ri_df["_CaseIndex"].nunique())
        _total_recovered = int(_ri_df.loc[_ri_df["Journey Status"].eq("Recovered"), "_CaseIndex"].nunique())
        _total_pending = int(_ri_df.loc[_ri_df["Journey Status"].eq("Pending / In Progress"), "_CaseIndex"].nunique())
        _total_untouched = int(_ri_df.loc[_ri_df["Journey Status"].eq("Untouched"), "_CaseIndex"].nunique())

        st.markdown(
            """
            <style>
            .ri-summary{display:flex;gap:8px;margin:0 0 7px 0;flex-wrap:wrap}
            .ri-pill{border:1px solid #E2E8F0;border-radius:8px;background:#FFF;padding:4px 8px;font-size:10px;color:#475467}
            .ri-pill b{font-size:12px;color:#101828;margin-right:3px}
            .ri-story-wrap{border:1px solid #E2E8F0;border-radius:12px;overflow:hidden;background:#FFF;box-shadow:0 1px 2px rgba(16,24,40,.03)}
            .ri-story-row{display:grid;grid-template-columns:minmax(125px,1.05fr) minmax(205px,1.65fr) 58px 64px;gap:7px;align-items:center;padding:6px 9px;border-bottom:1px solid #EEF2F6;color:#0F172A;min-height:39px}
            .ri-story-row:last-child{border-bottom:0}
            .ri-reason,.ri-path,.ri-state{display:flex;flex-direction:column;line-height:1.16}
            .ri-reason b{font-size:11.5px}.ri-reason span,.ri-path span,.ri-state span{font-size:9px;color:#667085;margin-top:2px}
            .ri-path{font-size:10.5px;color:#344054}.ri-path b{color:#101828}
            .ri-state{text-align:center;align-items:center}.ri-state b{font-size:12px;color:#B54708}.ri-untouched b{color:#B42318}
            </style>
            """ +
            f"<div class='ri-summary'>"
            f"<div class='ri-pill'><b>{_total_rej_story:,}</b> rejected cases</div>"
            f"<div class='ri-pill'><b>{_total_recovered:,}</b> recovered later</div>"
            f"<div class='ri-pill'><b>{_total_pending:,}</b> pending after move</div>"
            f"<div class='ri-pill'><b>{_total_untouched:,}</b> untouched after rejection</div>"
            f"</div><div class='ri-story-wrap'>{''.join(_ri_html)}</div>",
            unsafe_allow_html=True,
        )
    else:
        st.info("No rejected-attempt journey data for the selected filters.")


with _second_right:
    # --------------------------------------------------------
    # 44B. BANK-WISE DISBURSALS
    # --------------------------------------------------------
    st.markdown("### 5. Lender Disbursement Performance")
    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

    _bank_disb = filtered.loc[
        filtered["_confirmed_disbursed"]
    ].copy()

    if selected_month != "All Months":
        _bank_disb = _bank_disb.loc[
            _bank_disb["_disbursed_dt"].between(
                report_start,
                report_end,
                inclusive="both",
            )
        ].copy()

    if _bank_disb.empty:
        st.info("No confirmed disbursals for the selected filters.")
    else:
        _bank_disb["_BankDisplay"] = (
            clean_series(_bank_disb["Bank/NBFC"])
            .replace("", "Unspecified")
        )
        _bank_disb["_DisbAmt"] = pd.to_numeric(
            _bank_disb["Disbursed Amount"],
            errors="coerce",
        ).fillna(0).clip(lower=0)
        _bank_disb["_ProjectValue"] = pd.to_numeric(
            _bank_disb["Project Value"],
            errors="coerce",
        ).fillna(0).clip(lower=0)

        _bank_summary = (
            _bank_disb
            .groupby("_BankDisplay")
            .agg(
                Cases=("LEAD ID", "count"),
                DisbursedAmount=("_DisbAmt", "sum"),
                ProjectValue=("_ProjectValue", "sum"),
            )
            .reset_index()
            .sort_values(
                ["DisbursedAmount", "_BankDisplay"],
                ascending=[False, True],
            )
        )

        _bank_custom = np.column_stack(
            [
                _bank_summary["Cases"],
                _bank_summary["ProjectValue"].map(_format_money_compact),
                _bank_summary["DisbursedAmount"].map(_format_money_compact),
            ]
        )

        _bank_palette = [
            "#1570EF", "#12B76A", "#7F56D9", "#F79009",
            "#F04438", "#06AED4", "#6172F3", "#2E90FA",
            "#039855", "#DC6803", "#D92D20", "#6938EF",
        ]
        _bank_colors = [
            _bank_palette[i % len(_bank_palette)]
            for i in range(len(_bank_summary))
        ]

        fig = go.Figure(
            go.Bar(
                x=_bank_summary["_BankDisplay"],
                y=_bank_summary["DisbursedAmount"],
                marker=dict(
                    color=_bank_colors,
                    line=dict(
                        color="rgba(255,255,255,.92)",
                        width=1,
                    ),
                ),
                text=_bank_summary["DisbursedAmount"].map(
                    _format_money_compact
                ),
                textposition="outside",
                textfont=dict(color="#000000", size=12, family="Arial Black"),
                cliponaxis=False,
                customdata=_bank_custom,
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    "Disbursed Value: <b>%{customdata[2]}</b><br>"
                    "Disbursed Cases: <b>%{customdata[0]}</b><br>"
                    "Project Value: <b>%{customdata[1]}</b>"
                    "<extra></extra>"
                ),
            )
        )
        fig.update_layout(
            # Match the visual height of Stage × Region so Row 1 is balanced.
            height=245,
            margin=dict(l=2, r=2, t=18, b=2),
            xaxis_title="",
            yaxis_title="",
            showlegend=False,
            paper_bgcolor="#FFFFFF",
            plot_bgcolor="#FFFFFF",
            bargap=.34,
        )
        fig.update_xaxes(
            tickangle=0,
            tickfont=dict(size=9, color="#050B18"),
            showgrid=False,
            fixedrange=True,
        )
        fig.update_yaxes(
            visible=False,
            rangemode="tozero",
            fixedrange=True,
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
            config={
                "displaylogo": False,
                "displayModeBar": False,
            },
        )

    # --------------------------------------------------------
    # 44C. REGION × BANK ACCEPTANCE — COMPACT + HOVER + CLICK POPUP
    #
    # Cell:
    #   Overall acceptance % for that Region × Bank
    #
    # Hover:
    #   Only attempts that were ACTUALLY USED for that Region × Bank.
    #   Example: if only A1, A2 and A4 exist, hover shows only A1/A2/A4.
    #
    # Click:
    #   Opens an Excel-style drill-down popup similar to Table 5 with:
    #   - overall acceptance
    #   - used-attempt breakdown
    #   - complete underlying case data
    #   - CSV download
    #
    # Acceptance:
    #   attempt's own Stage == Approved
    # --------------------------------------------------------
    st.markdown("<div id='region-bank-card' style='height:0;margin-top:4px'></div>", unsafe_allow_html=True)
    st.markdown("### 7. Regional Lender Acceptance Rate")
    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

    _rb_attempt_parts = []

    _rb_ordinals = {
        1: "1st",
        2: "2nd",
        3: "3rd",
        4: "4th",
        5: "5th",
        6: "6th",
        7: "7th",
    }

    for _attempt_no in range(1, 8):
        _suffix = _rb_ordinals[_attempt_no]
        _provider_col = f"{_suffix} Provider"
        _stage_col = f"{_suffix} Stage"
        _substage_col = f"{_suffix} Substage"
        _date_col = f"{_suffix} Date"

        if _provider_col not in period_login_cases.columns:
            continue

        _provider_s = clean_series(period_login_cases[_provider_col])
        _valid_attempt = _provider_s.ne("")

        if not _valid_attempt.any():
            continue

        _part = pd.DataFrame(
            {
                "_CaseIndex": period_login_cases.loc[_valid_attempt].index,
                "LEAD ID": period_login_cases.loc[_valid_attempt, "LEAD ID"],
                "Region": (
                    clean_series(period_login_cases.loc[_valid_attempt, "Region"])
                    if "Region" in period_login_cases.columns
                    else ""
                ),
                "Attempt": _attempt_no,
                "Bank": _provider_s.loc[_valid_attempt],
                "Attempt Stage": (
                    clean_series(period_login_cases.loc[_valid_attempt, _stage_col])
                    if _stage_col in period_login_cases.columns
                    else ""
                ),
                "Attempt Substage": (
                    clean_series(period_login_cases.loc[_valid_attempt, _substage_col])
                    if _substage_col in period_login_cases.columns
                    else ""
                ),
                "Attempt Date": (
                    parse_dashboard_datetime(
                        period_login_cases.loc[_valid_attempt, _date_col]
                    )
                    if _date_col in period_login_cases.columns
                    else pd.NaT
                ),
            }
        )
        _rb_attempt_parts.append(_part)

    if _rb_attempt_parts:
        _rb_attempts = pd.concat(_rb_attempt_parts, ignore_index=True)

        _rb_attempts["Region"] = clean_series(_rb_attempts["Region"])
        _rb_attempts["Bank"] = clean_series(_rb_attempts["Bank"])

        _rb_attempts = _rb_attempts.loc[
            _rb_attempts["Region"].ne("")
            & _rb_attempts["Bank"].ne("")
            & ~_rb_attempts["Region"].str.casefold().isin(
                {"unknown", "nan", "none", "blank", "blank region"}
            )
        ].copy()

        _rb_attempts["Accepted"] = (
            clean_series(_rb_attempts["Attempt Stage"])
            .str.casefold()
            .eq("approved")
        )

        # ----------------------------------------------------
        # Overall Region × Bank performance
        # ----------------------------------------------------
        _rb_summary = (
            _rb_attempts
            .groupby(["Region", "Bank"], dropna=False)
            .agg(
                Attempts=("Attempt", "size"),
                Accepted=("Accepted", "sum"),
            )
            .reset_index()
        )

        _rb_summary["Acceptance Rate"] = np.where(
            _rb_summary["Attempts"] > 0,
            _rb_summary["Accepted"] / _rb_summary["Attempts"] * 100,
            0,
        )

        # ----------------------------------------------------
        # Attempt-level performance. Missing attempts are NOT created.
        # ----------------------------------------------------
        _rb_by_attempt = (
            _rb_attempts
            .groupby(["Region", "Bank", "Attempt"], dropna=False)
            .agg(
                Attempts=("Attempt", "size"),
                Accepted=("Accepted", "sum"),
            )
            .reset_index()
        )

        _rb_by_attempt["Acceptance Rate"] = np.where(
            _rb_by_attempt["Attempts"] > 0,
            _rb_by_attempt["Accepted"] / _rb_by_attempt["Attempts"] * 100,
            0,
        )

        _rb_preferred_regions = [
            "Secunderabad",
            "Warangal",
            "Nizamabad",
            "Rayalaseema",
            "Vijayawada",
            "Nellore",
            "Vizag",
        ]

        _rb_regions_available = (
            _rb_summary["Region"].dropna().astype(str).unique().tolist()
        )

        _rb_region_order = [
            _r for _r in _rb_preferred_regions
            if _r in _rb_regions_available
        ] + sorted(
            [
                _r for _r in _rb_regions_available
                if _r not in _rb_preferred_regions
            ],
            key=lambda _x: _x.casefold(),
        )

        _rb_bank_order = (
            _rb_summary.groupby("Bank")["Attempts"]
            .sum()
            .sort_values(ascending=False)
            .index
            .tolist()
        )

        # Wrap long lender names instead of stretching the chart.
        def _rb_wrap_bank_name(_name):
            _name = clean_text(_name)
            if not _name:
                return ""
            _words = _name.split()
            if len(_words) > 1:
                return "<br>".join(_words)
            if len(_name) > 11:
                _mid = len(_name) // 2
                return _name[:_mid] + "<br>" + _name[_mid:]
            return _name

        _rb_bank_ticktext = [_rb_wrap_bank_name(_b) for _b in _rb_bank_order]

        # ----------------------------------------------------
        # Build compact heatmap matrices.
        # ----------------------------------------------------
        _rb_z = []
        _rb_text = []
        _rb_hover = []

        for _region in _rb_region_order:
            _z_row = []
            _text_row = []
            _hover_row = []

            for _bank in _rb_bank_order:
                _overall = _rb_summary.loc[
                    _rb_summary["Region"].eq(_region)
                    & _rb_summary["Bank"].eq(_bank)
                ]

                if _overall.empty:
                    _z_row.append(np.nan)
                    _text_row.append("—")
                    _hover_row.append(
                        f"<b>{_region} × {_bank}</b><br>No attempts"
                    )
                    continue

                _overall_attempts = int(_overall.iloc[0]["Attempts"])
                _overall_accepted = int(_overall.iloc[0]["Accepted"])
                _overall_rate = float(_overall.iloc[0]["Acceptance Rate"])

                _z_row.append(_overall_rate)
                _text_row.append(f"{_overall_rate:.0f}%")

                _hover_lines = [
                    f"<b>{_region} × {_bank}</b>",
                    f"Overall: <b>{_overall_rate:.1f}%</b> "
                    f"({_overall_accepted}/{_overall_attempts} approved)",
                ]

                # ONLY attempts actually used are shown.
                _attempt_slice = (
                    _rb_by_attempt.loc[
                        _rb_by_attempt["Region"].eq(_region)
                        & _rb_by_attempt["Bank"].eq(_bank)
                    ]
                    .sort_values("Attempt")
                )

                if not _attempt_slice.empty:
                    _hover_lines.append("<br><b>Used attempts</b>")
                    for _, _a_row in _attempt_slice.iterrows():
                        _a = int(_a_row["Attempt"])
                        _a_total = int(_a_row["Attempts"])
                        _a_approved = int(_a_row["Accepted"])
                        _a_rate = float(_a_row["Acceptance Rate"])
                        _hover_lines.append(
                            f"A{_a}: <b>{_a_rate:.1f}%</b> "
                            f"({_a_approved}/{_a_total})"
                        )

                _hover_lines.append("<br><i>Click for case details</i>")
                _hover_row.append("<br>".join(_hover_lines))

            _rb_z.append(_z_row)
            _rb_text.append(_text_row)
            _rb_hover.append(_hover_row)

        _rb_fig = go.Figure(
            data=go.Heatmap(
                z=_rb_z,
                x=_rb_bank_order,
                y=_rb_region_order,
                zmin=0,
                zmax=100,
                colorscale=[
                    [0.00, "#FEE2E2"],
                    [0.35, "#FEF3C7"],
                    [0.60, "#DBEAFE"],
                    [1.00, "#DCFCE7"],
                ],
                showscale=False,
                # NOTE: Do not use Heatmap texttemplate here.
                # streamlit-plotly-events can load a Plotly.js build where
                # heatmap texttemplate is not rendered, which caused the
                # coloured cells to appear blank. Visible percentages are
                # added as annotations below, while the heatmap itself keeps
                # hover + click behaviour.
                customdata=_rb_hover,
                hovertemplate="%{customdata}<extra></extra>",
                xgap=1,
                ygap=1,
            )
        )

        # ----------------------------------------------------
        # Always-visible overall % + MINI A1/A2/A3 BARS
        #
        # Stream 2.6: this follows the "Heatmap + Mini Attempt Bars"
        # reference.  We DO NOT print A1/A2/A3 text inside the cell.
        # Instead, each populated cell gets up to three tiny bars:
        #   A1 = blue, A2 = green, A3 = orange.
        # Bar length = that attempt's acceptance rate.
        # Missing attempts are not drawn.
        # Full attempt details remain available on hover.
        # ----------------------------------------------------
        _rb_attempt_bar_colors = {
            1: "#2F80ED",   # A1
            2: "#2ECFA4",   # A2
            3: "#F5A623",   # A3
        }

        for _ri, _region in enumerate(_rb_region_order):
            for _bi, _bank in enumerate(_rb_bank_order):
                _label = _rb_text[_ri][_bi]
                if _label == "—":
                    continue

                # Main overall acceptance value — large, bold and uncluttered.
                _rb_fig.add_annotation(
                    x=_bi,
                    y=_ri - 0.12,
                    xref="x",
                    yref="y",
                    text=f"<b>{_label}</b>",
                    showarrow=False,
                    font=dict(size=11, color="#050B18"),
                    xanchor="center",
                    yanchor="middle",
                )

                # Tiny A1/A2/A3 bars live in the lower part of the heatmap cell.
                # Plotly categorical heatmaps map categories to 0,1,2... so we
                # can position compact shapes precisely without widening cells.
                _mini_slice = _rb_by_attempt.loc[
                    _rb_by_attempt["Region"].eq(_region)
                    & _rb_by_attempt["Bank"].eq(_bank)
                    & _rb_by_attempt["Attempt"].isin([1, 2, 3])
                ].sort_values("Attempt")

                if not _mini_slice.empty:
                    _mini_lookup = {
                        int(_r["Attempt"]): float(_r["Acceptance Rate"])
                        for _, _r in _mini_slice.iterrows()
                    }

                    # Track is intentionally short so all three bars fit cleanly.
                    _track_left = _bi - 0.34
                    _track_right = _bi + 0.34
                    _track_width = _track_right - _track_left
                    _bar_y = _ri + 0.23
                    _bar_h = 0.055
                    _gap = 0.018
                    _slot_w = (_track_width - (2 * _gap)) / 3

                    for _slot, _attempt_no in enumerate((1, 2, 3)):
                        if _attempt_no not in _mini_lookup:
                            continue

                        _rate = max(0.0, min(100.0, _mini_lookup[_attempt_no]))
                        _slot_x0 = _track_left + _slot * (_slot_w + _gap)
                        _slot_x1 = _slot_x0 + _slot_w

                        # Light neutral track.
                        _rb_fig.add_shape(
                            type="rect",
                            xref="x",
                            yref="y",
                            x0=_slot_x0,
                            x1=_slot_x1,
                            y0=_bar_y - _bar_h,
                            y1=_bar_y + _bar_h,
                            line=dict(width=0),
                            fillcolor="rgba(255,255,255,0.72)",
                            layer="above",
                        )

                        # Filled portion = attempt acceptance rate.
                        if _rate > 0:
                            _rb_fig.add_shape(
                                type="rect",
                                xref="x",
                                yref="y",
                                x0=_slot_x0,
                                x1=_slot_x0 + (_slot_w * _rate / 100.0),
                                y0=_bar_y - _bar_h,
                                y1=_bar_y + _bar_h,
                                line=dict(width=0),
                                fillcolor=_rb_attempt_bar_colors[_attempt_no],
                                layer="above",
                            )

        # Small legend: it explains the mini-bars once instead of repeating
        # A1/A2/A3 text in every heatmap cell.
        _rb_fig.add_annotation(
            x=0.5,
            y=-0.11,
            xref="paper",
            yref="paper",
            text=(
                "<span style='color:#2F80ED'>■</span> <b>A1</b>"
                "&nbsp;&nbsp;&nbsp;"
                "<span style='color:#2ECFA4'>■</span> <b>A2</b>"
                "&nbsp;&nbsp;&nbsp;"
                "<span style='color:#F5A623'>■</span> <b>A3</b>"
                "&nbsp;&nbsp; <span style='color:#667085'>bar length = acceptance %</span>"
            ),
            showarrow=False,
            font=dict(size=9, color="#050B18"),
            xanchor="center",
        )

        _rb_fig.update_layout(
            # Professional compact matrix: enough breathing room for labels/bars,
            # while staying aligned with the left-side recovery table.
            margin=dict(l=8, r=8, t=8, b=30),
            height=max(248, 54 + len(_rb_region_order) * 28),
            paper_bgcolor="#FFFFFF",
            plot_bgcolor="#FFFFFF",
            font=dict(color="#050B18", size=10),
            xaxis=dict(
                title=None,
                side="top",
                tickmode="array",
                tickvals=_rb_bank_order,
                ticktext=_rb_bank_ticktext,
                tickfont=dict(size=10, color="#111827"),
                tickangle=0,
                ticks="",
                showgrid=False,
                zeroline=False,
                showline=False,
                automargin=True,
                fixedrange=True,
            ),
            yaxis=dict(
                title=None,
                autorange="reversed",
                tickfont=dict(size=10, color="#111827"),
                ticks="",
                showgrid=False,
                zeroline=False,
                showline=False,
                automargin=True,
                fixedrange=True,
            ),
            hoverlabel=dict(
                bgcolor="white",
                font_size=12,
                font_color="#050B18",
                align="left",
            ),
        )

        # ----------------------------------------------------
        # Clickable rendering.
        # ----------------------------------------------------
        # Plotly heatmap cells themselves are not consistently returned by
        # Streamlit's point-selection event.  Add a transparent scatter marker
        # at the centre of every POPULATED Region × Bank cell.  The visual stays
        # exactly the same, while every real block becomes reliably clickable.
        _rb_click_x, _rb_click_y, _rb_click_custom = [], [], []
        for _ri, _region in enumerate(_rb_region_order):
            for _bi, _bank in enumerate(_rb_bank_order):
                if _rb_text[_ri][_bi]:
                    _rb_click_x.append(_bank)
                    _rb_click_y.append(_region)
                    _rb_click_custom.append([_region, _bank])

        if _rb_click_x:
            _rb_fig.add_trace(
                go.Scatter(
                    x=_rb_click_x,
                    y=_rb_click_y,
                    mode="markers",
                    customdata=_rb_click_custom,
                    marker=dict(size=24, opacity=0.01, color="#FFFFFF"),
                    hoverinfo="skip",
                    showlegend=False,
                    name="Region × Bank click targets",
                )
            )

        _rb_clicked_points = []
        _rb_event = st.plotly_chart(
            _rb_fig,
            use_container_width=True,
            on_select="rerun",
            selection_mode="points",
            config={
                "displaylogo": False,
                "displayModeBar": False,
                "scrollZoom": False,
            },
            key=(
                "region_bank_acceptance_native_"
                f"{_dashboard_filter_signature}_"
                f"{st.session_state['region_bank_click_nonce']}"
            ),
        )

        try:
            _rb_clicked_points = list(_rb_event.selection.points)
        except Exception:
            try:
                _rb_clicked_points = list(
                    _rb_event.get("selection", {}).get("points", [])
                )
            except Exception:
                _rb_clicked_points = []

        # ----------------------------------------------------
        # Region × Bank popup — same drill-down idea as Table 5.
        # ----------------------------------------------------
        if _rb_clicked_points:
            _rb_point = _rb_clicked_points[0]
            _rb_selection_signature = repr(_rb_point)
            if st.session_state.get("region_bank_last_handled") == _rb_selection_signature:
                _rb_point = None
            else:
                st.session_state["region_bank_last_handled"] = _rb_selection_signature

        if _rb_clicked_points and _rb_point is not None:
            _rb_custom = _rb_point.get("customdata") if isinstance(_rb_point, dict) else None
            if isinstance(_rb_custom, (list, tuple)) and len(_rb_custom) >= 2:
                _rb_clicked_region = clean_text(_rb_custom[0])
                _rb_clicked_bank = clean_text(_rb_custom[1])
            else:
                _rb_clicked_bank = clean_text(_rb_point.get("x"))
                _rb_clicked_region = clean_text(_rb_point.get("y"))

            if (
                _rb_clicked_bank in _rb_bank_order
                and _rb_clicked_region in _rb_region_order
            ):
                # Consume this selection before opening the dialog.  Closing via
                # Streamlit's native top-right X causes the next run to render a
                # fresh chart key with NO selected/red block and NO stale popup.
                st.session_state["region_bank_click_nonce"] += 1
                st.session_state["region_bank_last_handled"] = None

                _rb_detail = _rb_attempts.loc[
                    _rb_attempts["Region"].eq(_rb_clicked_region)
                    & _rb_attempts["Bank"].eq(_rb_clicked_bank)
                ].copy()

                if not _rb_detail.empty:
                    _rb_popup_breakdown = (
                        _rb_detail
                        .groupby("Attempt", dropna=False)
                        .agg(
                            Attempts=("Attempt", "size"),
                            Accepted=("Accepted", "sum"),
                        )
                        .reset_index()
                        .sort_values("Attempt")
                    )
                    _rb_popup_breakdown["Acceptance %"] = np.where(
                        _rb_popup_breakdown["Attempts"] > 0,
                        _rb_popup_breakdown["Accepted"]
                        / _rb_popup_breakdown["Attempts"] * 100,
                        0,
                    ).round(1)
                    _rb_popup_breakdown["Attempt"] = (
                        "A" + _rb_popup_breakdown["Attempt"].astype(int).astype(str)
                    )

                    _rb_total_attempts = int(len(_rb_detail))
                    _rb_total_accepted = int(_rb_detail["Accepted"].sum())
                    _rb_overall_rate = (
                        _rb_total_accepted / _rb_total_attempts * 100
                        if _rb_total_attempts else 0
                    )

                    _rb_case_indices = (
                        _rb_detail["_CaseIndex"]
                        .dropna()
                        .unique()
                        .tolist()
                    )
                    _rb_case_source = period_login_cases.loc[
                        period_login_cases.index.isin(_rb_case_indices)
                    ].copy()

                    _rb_case_cols_candidates = [
                        "LEAD ID",
                        "Lead Code",
                        "Customer Name",
                        "Mobile",
                        "Region",
                        "Bank/NBFC",
                        "Consultant Name",
                        "Current Status",
                        "Loan Sub Stage",
                        "Login Done On",
                        "Disbursed At",
                        "Project Value",
                        "Disbursed Amount",
                        "Login Attempts",
                    ]
                    _rb_case_cols = [
                        _c for _c in _rb_case_cols_candidates
                        if _c in _rb_case_source.columns
                    ]
                    if not _rb_case_cols:
                        _rb_case_cols = [
                            _c for _c in _rb_case_source.columns
                            if not str(_c).startswith("_")
                        ][:15]

                    _rb_case_show = (
                        _rb_case_source[_rb_case_cols]
                        .reset_index(drop=True)
                    )

                    @st.dialog(
                        f"{_rb_clicked_region} × {_rb_clicked_bank}",
                        width="large",
                    )
                    def _show_region_bank_popup():
                        st.markdown(
                            (
                                "<div style='display:grid;"
                                "grid-template-columns:repeat(3,minmax(0,1fr));"
                                "gap:10px;margin:0 0 14px 0;'>"
                                "<div style='padding:10px 12px;border:1px solid #E4E7EC;"
                                "border-radius:10px;text-align:center;'>"
                                "<div style='font-size:11px;color:#667085;'>ACCEPTANCE</div>"
                                f"<div style='font-size:20px;font-weight:800;color:#0B1833;'>"
                                f"{_rb_overall_rate:.1f}%</div></div>"
                                "<div style='padding:10px 12px;border:1px solid #E4E7EC;"
                                "border-radius:10px;text-align:center;'>"
                                "<div style='font-size:11px;color:#667085;'>ATTEMPTS</div>"
                                f"<div style='font-size:20px;font-weight:800;color:#0B1833;'>"
                                f"{_rb_total_attempts:,}</div></div>"
                                "<div style='padding:10px 12px;border:1px solid #E4E7EC;"
                                "border-radius:10px;text-align:center;'>"
                                "<div style='font-size:11px;color:#667085;'>APPROVED</div>"
                                f"<div style='font-size:20px;font-weight:800;color:#0B1833;'>"
                                f"{_rb_total_accepted:,}</div></div>"
                                "</div>"
                            ),
                            unsafe_allow_html=True,
                        )

                        st.markdown("##### Used Attempt Performance")
                        st.dataframe(
                            _rb_popup_breakdown.style.format(
                                {"Acceptance %": "{:.1f}%"}
                            ).set_properties(
                                **{
                                    "text-align": "center",
                                    "vertical-align": "middle",
                                }
                            ).set_table_styles(
                                [{
                                    "selector": "th",
                                    "props": [
                                        ("text-align", "center"),
                                        ("vertical-align", "middle"),
                                    ],
                                }]
                            ),
                            use_container_width=True,
                            hide_index=True,
                            height=min(
                                300,
                                42 + len(_rb_popup_breakdown) * 36,
                            ),
                        )

                        st.markdown(
                            f"##### Cases ({len(_rb_case_show):,})"
                        )

                        # Table-5-like interactive popup table with filters.
                        if not _rb_case_show.empty:
                            _rb_gb = GridOptionsBuilder.from_dataframe(
                                _rb_case_show
                            )
                            _rb_gb.configure_default_column(
                                filter=True,
                                sortable=True,
                                resizable=True,
                                minWidth=110,
                            )
                            _rb_gb.configure_grid_options(
                                pagination=True,
                                paginationPageSize=15,
                                domLayout="normal",
                            )
                            _rb_grid_options = _rb_gb.build()

                            AgGrid(
                                _rb_case_show,
                                gridOptions=_rb_grid_options,
                                data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                                update_mode=GridUpdateMode.NO_UPDATE,
                                fit_columns_on_grid_load=False,
                                enable_enterprise_modules=False,
                                theme="streamlit",
                                height=390,
                                key=(
                                    "rb_popup_grid_"
                                    f"{_rb_clicked_region}_"
                                    f"{_rb_clicked_bank}_"
                                    f"{_dashboard_filter_signature}"
                                ),
                            )

                        _rb_download = (
                            f"region_bank_{_rb_clicked_region}_{_rb_clicked_bank}.csv"
                            .replace(" ", "_")
                            .replace("/", "-")
                        )

                        st.download_button(
                            "Download cases",
                            data=_rb_case_show.to_csv(index=False).encode("utf-8-sig"),
                            file_name=_rb_download,
                            mime="text/csv",
                            use_container_width=True,
                            key=(
                                "download_region_bank_"
                                f"{_rb_clicked_region}_"
                                f"{_rb_clicked_bank}"
                            ),
                        )

                    _show_region_bank_popup()

        st.caption(
            "A1 60% (6/10) means 6 of 10 cases sent to that bank as the first "
            "attempt were approved. Hover hides attempts that were never used."
        )

    else:
        st.info(
            "No A1–A7 bank-attempt records for the selected filters."
        )

# ============================================================
# 45. TABLE 5 — BELOW ROW 2 (NOT PART OF ROW 2)
# ============================================================
# ============================================================
# 45. ANALYSIS 5 — ATTEMPT PRIORITY × BANK OUTCOME
#
# Question answered:
# "How many cases did we prioritise to each bank at each attempt,
#  and how many of those were approved vs rejected?"
#
# Rows    = 1st / 2nd / 3rd / 4th / 5th attempt
# Columns = actual Bank / NBFC names
# Cell    = Approved / Rejected (Total cases given that priority)
# ============================================================

ATTEMPT_SUFFIXES = ["1st", "2nd", "3rd", "4th", "5th"]

attempt_rows = []

for attempt_number, suffix in enumerate(
    ATTEMPT_SUFFIXES,
    start=1,
):
    provider_col = f"{suffix} Provider"
    stage_col = f"{suffix} Stage"
    substage_col = f"{suffix} Substage"
    date_col = f"{suffix} Date"

    if provider_col not in period_login_cases.columns:
        continue

    provider_series = clean_series(
        period_login_cases[provider_col]
    )

    valid_mask = provider_series.ne("")

    if not valid_mask.any():
        continue

    temp = pd.DataFrame(
        {
            # Preserve the original source-row index so the popup can
            # retrieve the exact underlying Credit records for this cell.
            "_CaseIndex":
                period_login_cases.loc[valid_mask].index,

            "LEAD ID":
                period_login_cases.loc[valid_mask, "LEAD ID"],

            "Region":
                period_login_cases.loc[valid_mask, "Region"],

            "Attempt":
                attempt_number,

            "Provider":
                provider_series.loc[valid_mask],

            "Stage":
                (
                    clean_series(
                        period_login_cases.loc[valid_mask, stage_col]
                    )
                    if stage_col in period_login_cases.columns
                    else ""
                ),

            "Substage":
                (
                    clean_series(
                        period_login_cases.loc[valid_mask, substage_col]
                    )
                    if substage_col in period_login_cases.columns
                    else ""
                ),

            "Attempt Date":
                (
                    parse_dashboard_datetime(
                        period_login_cases.loc[valid_mask, date_col]
                    )
                    if date_col in period_login_cases.columns
                    else pd.NaT
                ),
        }
    )

    attempt_rows.append(temp)


if attempt_rows:
    attempt_month = pd.concat(
        attempt_rows,
        ignore_index=True,
    )
else:
    attempt_month = pd.DataFrame(
        columns=[
            "_CaseIndex",
            "LEAD ID",
            "Region",
            "Attempt",
            "Provider",
            "Stage",
            "Substage",
            "Attempt Date",
        ]
    )


def classify_attempt_outcome(stage, substage):
    stage = clean_text(stage).casefold()
    substage = clean_text(substage).casefold()

    # Approval is determined from the attempt's own stage.
    if stage == "approved":
        return "Approved"

    if stage == "rejected":
        return "Rejected"

    if stage == "login done":
        return "Login Done"

    if stage == "documents submitted":
        return "Documents Submitted"

    # Do not manufacture an approval purely from substage.
    if substage == "disbursed":
        return "Other / Review"

    if not stage:
        return "Blank / Unconfirmed"

    return stage.title()


if not attempt_month.empty:
    attempt_month["Outcome"] = attempt_month.apply(
        lambda row: classify_attempt_outcome(
            row["Stage"],
            row["Substage"],
        ),
        axis=1,
    )


# Compact Table 5 header/filter row: no oversized banner or wasted vertical gap.
st.markdown(
    """
    <style>
    div[data-testid="stSelectbox"] { margin-bottom: 0 !important; }
    div[data-testid="stSelectbox"] label {
        font-weight: 800 !important;
        color: #0B1833 !important;
        margin-bottom: 2px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if attempt_month.empty:
    st.markdown(
        f"### 8. Lender Attempt Performance by Priority — {report_label}"
    )
    st.info("No lender-attempt data for the selected reporting period.")

else:
    # Region filter is intentionally local to Analysis 5.
    attempt_regions = sorted(
        [
            x
            for x in (
                attempt_month["Region"]
                .fillna("")
                .astype(str)
                .str.strip()
                .unique()
            )
            if x
        ]
    )

    # Table 5 title and Region filter share one clean horizontal line.
    # Dedicated marker lets us override older/global dark row styling safely.
    st.markdown(
        """
        <style>
        /* TABLE 5 FINAL: pure white heading/filter strip — never a navy banner. */
        div[data-testid="stHorizontalBlock"]:has(#table5-clean-header),
        div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) > div,
        div[data-testid="stVerticalBlock"]:has(#table5-clean-header),
        div[data-testid="stVerticalBlock"]:has(#table5-clean-header) > div,
        div[data-testid="stElementContainer"]:has(#table5-clean-header),
        div[data-testid="stVerticalBlockBorderWrapper"]:has(#table5-clean-header) {
            background:#FFFFFF !important;
            background-color:#FFFFFF !important;
            background-image:none !important;
            border:0 !important;
            box-shadow:none !important;
        }
        #table5-clean-header {height:0 !important; margin:0 !important; padding:0 !important;}
        /* This beats the old global FILTER GLASS BAR selector. */
        div[data-testid="stHorizontalBlock"]:has(#table5-clean-header):has(div[data-testid="stSelectbox"]) {
            background:#FFFFFF !important;
            background-color:#FFFFFF !important;
            background-image:none !important;
            border:0 !important;
            border-radius:0 !important;
            box-shadow:none !important;
            padding:2px 0 5px 0 !important;
            margin:0 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) > div[data-testid="stColumn"] {
            background:#FFFFFF !important;
            background-color:#FFFFFF !important;
            background-image:none !important;
            border:0 !important;
            box-shadow:none !important;
        }
        div[data-testid="stVerticalBlock"]:has(#table5-clean-header) h3 {
            color:#111827 !important;
            font-weight:900 !important;
            letter-spacing:-0.35px !important;
            margin:0 !important;
            padding:0 !important;
            line-height:1.08 !important;
        }
        div[data-testid="stVerticalBlock"]:has(#table5-clean-header) div[data-testid="stSelectbox"] {
            margin:0 !important;
            padding:0 !important;
        }
        div[data-testid="stVerticalBlock"]:has(#table5-clean-header) div[data-testid="stSelectbox"] label {
            color:#475467 !important;
            font-size:11px !important;
            font-weight:800 !important;
            margin-bottom:3px !important;
        }
        div[data-testid="stVerticalBlock"]:has(#table5-clean-header) div[data-baseweb="select"] > div {
            background:#FFFFFF !important;
            color:#111827 !important;
            border:1px solid #D0D5DD !important;
            border-radius:9px !important;
            min-height:38px !important;
            box-shadow:0 1px 2px rgba(16,24,40,.04) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    table_filter_left, table_filter_right = st.columns(
        [5.15, 0.85],
        vertical_alignment="center",
        gap="small",
    )

    with table_filter_left:
        # IMPORTANT: marker lives inside this column so :has(#table5-clean-header)
        # targets the actual heading + Region-filter horizontal row.
        st.markdown('<div id="table5-clean-header"></div>', unsafe_allow_html=True)
        st.markdown(
            f"### 8. Lender Attempt Performance by Priority — {report_label}"
        )

    with table_filter_right:
        attempt_region_filter = st.selectbox(
            "Region",
            ["All Regions"] + attempt_regions,
            key="credit_attempt_region_filter_v2",
        )

    attempt_table_source = attempt_month.copy()

    if attempt_region_filter != "All Regions":
        attempt_table_source = attempt_table_source[
            attempt_table_source["Region"].eq(
                attempt_region_filter
            )
        ].copy()

    # Banks ordered by number of attempt assignments.
    # NO BANK WHITELIST / TOP-N LIMIT.
    # Every nonblank provider found in the attempt data is included.
    provider_order = (
        attempt_table_source
        .loc[attempt_table_source["Provider"].ne("")]
        .groupby("Provider")
        .size()
        .sort_values(ascending=False)
        .index
        .tolist()
    )

    attempt_order = sorted(
        attempt_table_source["Attempt"]
        .dropna()
        .astype(int)
        .unique()
        .tolist()
    )

    def attempt_label(number):
        labels = {
            1: "1st Attempt",
            2: "2nd Attempt",
            3: "3rd Attempt",
            4: "4th Attempt",
            5: "5th Attempt",
        }
        return labels.get(
            int(number),
            f"{int(number)}th Attempt",
        )

    def outcome_numbers(
        source,
        attempt=None,
        provider=None,
    ):
        subset = source

        if attempt is not None:
            subset = subset[
                subset["Attempt"].eq(attempt)
            ]

        if provider is not None:
            subset = subset[
                subset["Provider"].eq(provider)
            ]

        approved = int(
            subset["Outcome"].eq("Approved").sum()
        )

        rejected = int(
            subset["Outcome"].eq("Rejected").sum()
        )

        total = int(len(subset))

        return approved, rejected, total

    def outcome_cell_text(
        approved,
        rejected,
        total,
    ):
        remaining = max(
            int(total) - int(approved) - int(rejected),
            0,
        )

        if total:
            approved_pct = approved / total * 100
            rejected_pct = rejected / total * 100
            remaining_pct = remaining / total * 100
        else:
            approved_pct = rejected_pct = remaining_pct = 0.0

        # Compact professional cell:
        # first line = Approved / Rejected / Remaining
        # second line = percentage shares
        # third line = total underlying cases
        return (
            f"{approved:,} / {rejected:,} / {remaining:,}\n"
            f"{approved_pct:.1f}% / {rejected_pct:.1f}% / "
            f"{remaining_pct:.1f}%\n"
            f"{total:,}"
        )

    if not provider_order or not attempt_order:
        st.info(
            "No attempt/provider records for the selected region."
        )

    else:
        matrix_rows = []

        for attempt in attempt_order:
            row = {
                "Attempt Priority": attempt_label(attempt),
            }

            for provider in provider_order:
                approved, rejected, total = outcome_numbers(
                    attempt_table_source,
                    attempt=attempt,
                    provider=provider,
                )

                row[provider] = outcome_cell_text(
                    approved,
                    rejected,
                    total,
                )

            approved, rejected, total = outcome_numbers(
                attempt_table_source,
                attempt=attempt,
            )

            row["Total"] = outcome_cell_text(
                approved,
                rejected,
                total,
            )

            matrix_rows.append(row)

        # Total row is display-only. Provider/attempt drill-down is available
        # from the attempt rows above.
        total_row = {
            "Attempt Priority": "Total",
        }

        for provider in provider_order:
            approved, rejected, total = outcome_numbers(
                attempt_table_source,
                provider=provider,
            )

            total_row[provider] = outcome_cell_text(
                approved,
                rejected,
                total,
            )

        approved, rejected, total = outcome_numbers(
            attempt_table_source
        )

        total_row["Total"] = outcome_cell_text(
            approved,
            rejected,
            total,
        )

        matrix_rows.append(total_row)

        attempt_matrix_df = pd.DataFrame(
            matrix_rows
        )

        # ------------------------------------------------------------
        # TABLE 5 — EXACT REFERENCE-STYLE COLORED METRICS + SAFE CLICK
        # ------------------------------------------------------------
        # This uses AG Grid only for Table 5 presentation/click handling.
        # Business calculations above are unchanged.
        #
        # Cell format:
        #   Approved / Rejected / Remaining
        #   Approved% / Rejected% / Remaining%
        #   Total
        #
        # Colors:
        #   Approved  = green
        #   Rejected  = red
        #   Remaining = blue
        #
        # Clicking a provider cell selects ONLY that row and records the
        # provider column clicked. The popup is then built from the current
        # filtered attempt_table_source exactly as before.

        # Hidden technical columns used only for safe click routing.
        attempt_matrix_df["_AttemptNumber"] = [
            *attempt_order,
            None,
        ]
        attempt_matrix_df["_ClickedProvider"] = ""

        # A nonce is part of the grid key. Closing the popup increments it,
        # which creates a fresh unselected grid and prevents auto-reopening.
        if "credit_table5_click_nonce" not in st.session_state:
            st.session_state["credit_table5_click_nonce"] = 0

        table5_widget_key = (
            f"credit_attempt_matrix_ag_"
            f"{_dashboard_filter_signature}_"
            f"{re.sub(r'[^A-Za-z0-9]+', '_', str(attempt_region_filter))}_"
            f"{st.session_state['credit_table5_click_nonce']}"
        )

        # Render the three metrics with independent text colors exactly like
        # the approved reference image.
        table5_metric_renderer = JsCode(
            """
            class MetricCellRenderer {
                init(params) {
                    const raw = (params.value === null || params.value === undefined)
                        ? ""
                        : String(params.value);

                    const lines = raw.split("\\n");
                    const counts = (lines[0] || "").split("/").map(x => x.trim());
                    const pcts = (lines[1] || "").split("/").map(x => x.trim());
                    const total = (lines[2] || "").trim();

                    const green = "#00863F";
                    const red = "#F01820";
                    const blue = "#075FEA";
                    const navy = "#082F63";
                    const muted = "#4D718D";

                    const wrap = document.createElement("div");
                    wrap.style.width = "100%";
                    wrap.style.height = "100%";
                    wrap.style.display = "flex";
                    wrap.style.flexDirection = "column";
                    wrap.style.alignItems = "center";
                    wrap.style.justifyContent = "center";
                    wrap.style.lineHeight = "1.05";
                    wrap.style.fontFamily = "Inter, Arial, Helvetica, sans-serif";
                    wrap.style.cursor = "pointer";
                    wrap.style.userSelect = "none";

                    const first = document.createElement("div");
                    first.style.fontSize = "15px";
                    first.style.fontWeight = "900";
                    first.style.whiteSpace = "nowrap";
                    first.style.letterSpacing = "-0.15px";

                    const second = document.createElement("div");
                    second.style.fontSize = "10.5px";
                    second.style.fontWeight = "800";
                    second.style.whiteSpace = "nowrap";
                    second.style.marginTop = "2px";

                    const third = document.createElement("div");
                    third.style.fontSize = "12px";
                    third.style.fontWeight = "900";
                    third.style.color = navy;
                    third.style.marginTop = "4px";
                    third.style.padding = "0";
                    third.style.borderRadius = "0";
                    third.style.background = "transparent";
                    third.style.minWidth = "24px";
                    third.style.textAlign = "center";

                    function addMetric(target, value, color, isPct) {
                        const span = document.createElement("span");
                        const normalized = String(value || "")
                            .replace("%", "")
                            .replace(",", "")
                            .trim();
                        const isZero = normalized === "" || Number(normalized) === 0;

                        span.textContent = value || (isPct ? "0.0%" : "0");
                        span.style.color = isZero ? muted : color;
                        span.style.fontWeight = isZero ? "650" : "800";
                        target.appendChild(span);
                    }

                    function addSlash(target) {
                        const slash = document.createElement("span");
                        slash.textContent = " / ";
                        slash.style.color = "#9AAABC";
                        slash.style.fontWeight = "500";
                        target.appendChild(slash);
                    }

                    addMetric(first, counts[0], green, false);
                    addSlash(first);
                    addMetric(first, counts[1], red, false);
                    addSlash(first);
                    addMetric(first, counts[2], blue, false);

                    addMetric(second, pcts[0], green, true);
                    addSlash(second);
                    addMetric(second, pcts[1], red, true);
                    addSlash(second);
                    addMetric(second, pcts[2], blue, true);

                    third.textContent = total || "0";

                    wrap.appendChild(first);
                    wrap.appendChild(second);
                    wrap.appendChild(third);

                    this.eGui = wrap;
                }

                getGui() {
                    return this.eGui;
                }
            }
            """
        )

        # Record the exact provider column clicked, then select the row.
        # Total/label cells intentionally do not trigger a drill-down.
        table5_click_handler = JsCode(
            """
            function(params) {
                const field = params.colDef.field;
                const providerCols = params.context.providerColumns || [];

                if (
                    providerCols.includes(field) &&
                    params.data &&
                    params.data._AttemptNumber !== null &&
                    params.data._AttemptNumber !== undefined
                ) {
                    params.node.setDataValue("_ClickedProvider", field);
                    params.node.setSelected(true, true);
                }
            }
            """
        )

        table5_gb = GridOptionsBuilder.from_dataframe(
            attempt_matrix_df
        )

        table5_gb.configure_default_column(
            sortable=False,
            filter=False,
            resizable=True,
            suppressMovable=True,
            minWidth=118,
        )

        table5_gb.configure_column(
            "Attempt Priority",
            header_name="Attempt Priority",
            pinned="left",
            width=150,
            minWidth=150,
            maxWidth=150,
            cellStyle={
                "fontWeight": "700",
                "color": "#062E5C",
                "backgroundColor": "#F5F9FC",
                "display": "flex",
                "alignItems": "center",
                "justifyContent": "center",
                "textAlign": "center",
            },
        )

        for provider in provider_order:
            table5_gb.configure_column(
                provider,
                header_name=provider,
                cellRenderer=table5_metric_renderer,
                width=142,
                minWidth=142,
                maxWidth=142,
            )

        # Total is display-only.
        table5_gb.configure_column(
            "Total",
            header_name="Total",
            cellRenderer=table5_metric_renderer,
            width=160,
            minWidth=160,
            maxWidth=160,
            cellStyle={
                "backgroundColor": "#EAF4FB",
            },
        )

        table5_gb.configure_column(
            "_AttemptNumber",
            hide=True,
        )
        table5_gb.configure_column(
            "_ClickedProvider",
            hide=True,
        )

        table5_gb.configure_selection(
            selection_mode="single",
            use_checkbox=False,
        )

        table5_gb.configure_grid_options(
            onCellClicked=table5_click_handler,
            context={
                "providerColumns": provider_order,
            },
            # Match the compact reference-table geometry.
            rowHeight=48,
            headerHeight=42,
            suppressRowClickSelection=True,
            suppressCellFocus=True,
            suppressRowHoverHighlight=False,
            animateRows=False,
            ensureDomOrder=True,
            enableCellTextSelection=True,
            suppressSizeToFit=True,
            getRowStyle=JsCode(
                """
                function(params) {
                    if (params.data && params.data["Attempt Priority"] === "Total") {
                        return {
                            backgroundColor: "#EAF4FB",
                            fontWeight: "800"
                        };
                    }
                    return {
                        backgroundColor: "#FFFFFF"
                    };
                }
                """
            ),
        )

        table5_grid_options = table5_gb.build()

        # ------------------------------------------------------------
        # ------------------------------------------------------------
        # ------------------------------------------------------------
        # TABLE 5 VISUAL OVERRIDES — EXECUTIVE / PROFESSIONAL MATRIX
        # ------------------------------------------------------------
        table5_custom_css = {
            ".ag-root, .ag-root-wrapper, .ag-root-wrapper-body, .ag-layout-normal, .ag-body, .ag-body-viewport, .ag-center-cols-clipper, .ag-center-cols-viewport, .ag-center-cols-container, .ag-body-horizontal-scroll, .ag-body-horizontal-scroll-viewport": {
                "background-color": "#FFFFFF !important",
            },
            ".ag-root-wrapper": {
                "border": "1px solid #7FB5D8 !important",
                "border-radius": "4px !important",
                "box-shadow": "0 1px 4px rgba(24,74,113,0.035) !important",
                "overflow": "hidden !important",
                "background-color": "#FFFFFF !important",
                "font-family": "Inter, Segoe UI, Arial, sans-serif !important",
            },

            # Header band
            ".ag-header": {
                "background": "linear-gradient(180deg,#EAF5FC 0%,#DCEEF9 100%) !important",
                "border-bottom": "1px solid #7FB5D8 !important",
            },
            ".ag-header-cell": {
                "background-color": "transparent !important",
                "color": "#082F63 !important",
                "font-family": "Inter, Segoe UI, Arial, sans-serif !important",
                "font-size": "12px !important",
                "font-weight": "900 !important",
                "border-right": "1px solid #8FC0DF !important",
                "padding-left": "7px !important",
                "padding-right": "7px !important",
            },
            ".ag-header-cell-label": {
                "justify-content": "center !important",
                "width": "100% !important",
                "text-align": "center !important",
            },
            ".ag-header-cell-text": {
                "width": "100% !important",
                "text-align": "center !important",
                "font-weight": "900 !important",
                "color": "#082F63 !important",
                "letter-spacing": "0.05px !important",
            },

            # Body / spreadsheet grid
            ".ag-row": {
                "background-color": "#FFFFFF !important",
            },
            ".ag-row-even": {
                "background-color": "#FFFFFF !important",
            },
            ".ag-row-odd": {
                "background-color": "#FFFFFF !important",
            },
            ".ag-cell": {
                "font-family": "Inter, Segoe UI, Arial, sans-serif !important",
                "color": "#082F63 !important",
                "border-right": "1px solid #9CC7E2 !important",
                "border-bottom": "1px solid #9CC7E2 !important",
                "padding-left": "6px !important",
                "padding-right": "6px !important",
                "display": "flex !important",
                "align-items": "center !important",
                "justify-content": "center !important",
                "background-color": "#FFFFFF !important",
            },
            ".ag-header-cell:not(:last-child)": {
                "border-right": "1px solid #8FC0DF !important",
            },
            ".ag-cell:not(:last-child)": {
                "border-right": "1px solid #9CC7E2 !important",
            },
            ".ag-row:not(:last-child) .ag-cell": {
                "border-bottom": "1px solid #9CC7E2 !important",
            },

            # Click selection remains functional but has no ugly color overlay.
            ".ag-row-selected": {
                "background-color": "#FFFFFF !important",
            },
            ".ag-row-selected::before": {
                "background-color": "transparent !important",
                "opacity": "0 !important",
            },
            ".ag-row-selected .ag-cell": {
                "background-color": "#FFFFFF !important",
                "box-shadow": "none !important",
            },
            ".ag-cell-focus": {
                "outline": "none !important",
                "box-shadow": "none !important",
            },
            ".ag-cell-range-selected": {
                "background-color": "#FFFFFF !important",
                "outline": "none !important",
            },
            ".ag-row-hover .ag-cell": {
                "background-color": "#F8FCFF !important",
            },

            # Attempt Priority: pale blue fixed label column
            ".ag-pinned-left-header": {
                "background": "linear-gradient(180deg,#EAF5FC 0%,#DCEEF9 100%) !important",
                "border-right": "1px solid #7FB5D8 !important",
                "box-shadow": "none !important",
            },
            ".ag-pinned-left-cols-container": {
                "border-right": "1px solid #7FB5D8 !important",
                "box-shadow": "none !important",
            },
            ".ag-pinned-left-cols-container .ag-cell": {
                "background-color": "#F2F8FC !important",
                "color": "#082F63 !important",
                "font-size": "12px !important",
                "font-weight": "900 !important",
                "text-align": "center !important",
            },

            # Total row: full-width blue report band
            ".ag-row-last .ag-cell": {
                "background-color": "#E3F1FA !important",
                "font-weight": "900 !important",
                "border-top": "1px solid #7FB5D8 !important",
                "border-bottom": "1px solid #7FB5D8 !important",
            },

            # Total column: subtle blue summary band
            ".ag-center-cols-container .ag-cell:last-child": {
                "background-color": "#F0F7FB !important",
                "border-left": "1px solid #7FB5D8 !important",
            },
            ".ag-row-last .ag-cell:last-child": {
                "background-color": "#E3F1FA !important",
            },

            # Clean scroll behavior
            ".ag-body-vertical-scroll": {
                "display": "none !important",
                "width": "0 !important",
                "min-width": "0 !important",
            },
            ".ag-center-cols-viewport": {
                "overflow-y": "hidden !important",
            },
            ".ag-body-horizontal-scroll": {
                "min-height": "10px !important",
                "max-height": "10px !important",
            },
        }

        st.markdown(
            """
            <style>
            div[data-testid="stVerticalBlock"] {
                font-family: Inter, "Segoe UI", Arial, sans-serif;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        st.markdown(
            """
            <style>
            /* Table 5: white, exact-fit component with zero dead space below. */
            div[data-testid="stElementContainer"]:has(.ag-root-wrapper),
            div[data-testid="stElementContainer"]:has(.ag-root-wrapper) > div {
                margin-bottom: 0 !important;
                padding-bottom: 0 !important;
                background: #FFFFFF !important;
            }
            iframe[title="st_aggrid.agGrid"] {
                background: #FFFFFF !important;
                margin: 0 !important;
                padding: 0 !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        table5_response = AgGrid(
            attempt_matrix_df,
            gridOptions=table5_grid_options,
            update_mode=(
                GridUpdateMode.SELECTION_CHANGED
                | GridUpdateMode.VALUE_CHANGED
            ),
            data_return_mode=DataReturnMode.AS_INPUT,
            fit_columns_on_grid_load=False,
            allow_unsafe_jscode=True,
            enable_enterprise_modules=False,
            theme="streamlit",
            custom_css=table5_custom_css,
            # Exact-fit height: no empty dark/black area below the last row.
            # Geometry matches headerHeight=42 and rowHeight=48 above.
            height=(42 + (len(attempt_matrix_df) * 48) + 14),
            key=table5_widget_key,
        )

        table5_selected_rows = table5_response.get(
            "selected_rows",
            None,
        )

        if table5_selected_rows is not None:
            if isinstance(
                table5_selected_rows,
                pd.DataFrame,
            ):
                selected_table5_df = (
                    table5_selected_rows.copy()
                )
            else:
                selected_table5_df = pd.DataFrame(
                    table5_selected_rows
                )

            if not selected_table5_df.empty:
                selected_row = selected_table5_df.iloc[0]

                selected_attempt_raw = selected_row.get(
                    "_AttemptNumber"
                )
                selected_provider = clean_text(
                    selected_row.get(
                        "_ClickedProvider"
                    )
                )

                if (
                    pd.notna(selected_attempt_raw)
                    and selected_provider in provider_order
                ):
                    selected_attempt = int(
                        selected_attempt_raw
                    )

                    selected_cell_token = (
                        f"{_dashboard_filter_signature}|"
                        f"{attempt_region_filter}|"
                        f"{selected_attempt}|"
                        f"{selected_provider}"
                    )

                    st.session_state[
                        "credit_attempt_popup_request"
                    ] = {
                        "token": selected_cell_token,
                        "attempt": selected_attempt,
                        "provider": selected_provider,
                        "table_key": table5_widget_key,
                        "filter_signature": _dashboard_filter_signature,
                        "attempt_region": attempt_region_filter,
                    }



# ============================================================

# 46. CLICKABLE ATTEMPT CELL — POPUP DETAILS
# ============================================================


def _safe_unique_options(frame, column):
    if column not in frame.columns:
        return []
    return sorted([x for x in clean_series(frame[column]).unique() if x])


# Dialogs intentionally use Streamlit's native top-right X only.
@st.dialog("Attempt Case Details", width="large")
def show_attempt_case_dialog(
    clicked_attempt,
    clicked_provider,
    clicked_cell,
    period_login_cases,
):
    """
    Excel-style attempt-cell drill-down.

    IMPORTANT:
    - Loads the COMPLETE underlying population for the clicked matrix cell.
    - No analytical row limit is applied.
    - AG Grid pagination only changes how many rows are visible per page.
    - Column filters are built into the grid headers.
    - Filtered record count reflects the grid's filtered dataset.
    - Download exports ALL rows remaining after grid filters, not only
      the current visible page.
    """

    if clicked_cell is None or clicked_cell.empty:
        st.info("No underlying cases are available for this cell.")
        return

    clicked_total = int(len(clicked_cell))

    # --------------------------------------------------------
    # Recover the exact source rows.
    # --------------------------------------------------------
    if "_CaseIndex" in clicked_cell.columns:
        selected_indices = (
            clicked_cell["_CaseIndex"]
            .dropna()
            .tolist()
        )

        case_details = (
            period_login_cases
            .loc[selected_indices]
            .copy()
        )
    else:
        # Defensive fallback only. Normal execution always uses _CaseIndex.
        selected_leads = (
            clicked_cell["LEAD ID"]
            .fillna("")
            .astype(str)
            .tolist()
        )

        case_details = period_login_cases[
            period_login_cases["LEAD ID"]
            .fillna("")
            .astype(str)
            .isin(selected_leads)
        ].copy()

        lead_to_index = (
            case_details
            .reset_index()
            .drop_duplicates("LEAD ID")
            .set_index("LEAD ID")["index"]
            .to_dict()
        )

        clicked_cell = clicked_cell.copy()

        clicked_cell["_CaseIndex"] = (
            clicked_cell["LEAD ID"]
            .map(lead_to_index)
        )

        clicked_cell = clicked_cell[
            clicked_cell["_CaseIndex"].notna()
        ].copy()

    # --------------------------------------------------------
    # Attach the attempt-specific metadata to each full case.
    # --------------------------------------------------------
    attempt_meta_columns = [
        "_CaseIndex",
        "Attempt",
        "Provider",
        "Stage",
        "Substage",
        "Attempt Date",
        "Outcome",
    ]

    available_meta_columns = [
        col
        for col in attempt_meta_columns
        if col in clicked_cell.columns
    ]

    attempt_meta = (
        clicked_cell[available_meta_columns]
        .drop_duplicates(
            subset=["_CaseIndex"],
            keep="first",
        )
        .set_index("_CaseIndex")
    )

    case_details = case_details.join(
        attempt_meta,
        how="left",
    )

    case_details = case_details.rename(
        columns={
            "Provider": "Attempt Bank/NBFC",
            "Stage": "Attempt Status",
            "Substage": "Attempt Sub Status",
            "Outcome": "Attempt Outcome",
        }
    )

    # --------------------------------------------------------
    # KEEP ALL AVAILABLE CASE DETAILS.
    #
    # Important user requirement:
    # do not artificially restrict the drill-down to a short list
    # of columns. Put the most useful fields first, then append every
    # other master-data column that exists for the case.
    # --------------------------------------------------------
    priority_columns = [
        "LEAD ID",
        "Lead Name",
        "Mobile",
        "Mobile Number",
        "mobileNumber",
        "Region",
        "Lead District",
        "Vendor/Channel Partner Name",
        "Consultant Name",
        "Attempt",
        "Attempt Bank/NBFC",
        "Attempt Status",
        "Attempt Sub Status",
        "Attempt Outcome",
        "Attempt Date",
        "Bank/NBFC",
        "Current Status",
        "Loan Sub Stage",
        "Requested Date",
        "Login Done On",
        "Disbursed At",
        "Project Value",
        "Approved Amount",
        "Disbursed Amount",
        "Loan Pending Amount",
        "Credit Officer",
        "Fintech ID",
        "Bank Name",
        "IFSC Code",
        "Branch",
        "Comments",
        "Dynamic Pricing",
        "Source",
        "Login Attempts",
    ]

    ordered_columns = []

    for col in priority_columns:
        if (
            col in case_details.columns
            and col not in ordered_columns
        ):
            ordered_columns.append(col)

    for col in case_details.columns:
        if (
            col not in ordered_columns
            and not str(col).startswith("_")
        ):
            ordered_columns.append(col)

    case_details = case_details[
        ordered_columns
    ].copy()

    # AG Grid works best when timestamps/objects are serialized cleanly.
    for col in case_details.columns:
        if pd.api.types.is_datetime64_any_dtype(
            case_details[col]
        ):
            case_details[col] = (
                case_details[col]
                .dt.strftime("%d-%m-%Y %H:%M")
                .fillna("")
            )
        elif case_details[col].dtype == "object":
            case_details[col] = case_details[col].apply(
                lambda value:
                    ""
                    if value is None
                    else (
                        str(value)
                        if isinstance(
                            value,
                            (
                                dict,
                                list,
                                tuple,
                                set,
                            ),
                        )
                        else value
                    )
            )

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------
    st.markdown(
        f"### {html.escape(str(clicked_provider))} — "
        f"{attempt_label(clicked_attempt)}"
    )

    # --------------------------------------------------------
    # Page-size control only.
    # This NEVER limits the data loaded into the grid.
    # --------------------------------------------------------
    rows_per_page = st.selectbox(
        "Rows per page",
        [25, 50, 75, 100],
        index=1,
        key="aggrid_attempt_page_size",
    )

    # --------------------------------------------------------
    # AG GRID — EXCEL-STYLE HEADER FILTERS
    # --------------------------------------------------------
    gb = GridOptionsBuilder.from_dataframe(
        case_details
    )

    # Default behavior for EVERY column:
    # - sortable
    # - resizable
    # - floating header filter
    # - Excel-like set filter with search + select all
    gb.configure_default_column(
        sortable=True,
        filter=True,
        resizable=True,
        floatingFilter=True,
        minWidth=125,
    )

    # Use AG Grid set filters on categorical/text columns.
    # The miniFilter inside agSetColumnFilter provides the searchable
    # checklist + Select All behavior requested by the user.
    for col in case_details.columns:
        if pd.api.types.is_numeric_dtype(
            case_details[col]
        ):
            gb.configure_column(
                col,
                filter="agNumberColumnFilter",
            )
        else:
            gb.configure_column(
                col,
                filter="agSetColumnFilter",
                filterParams={
                    "buttons": [
                        "apply",
                        "clear",
                    ],
                    "closeOnApply": True,
                    "excelMode": "windows",
                    "suppressSelectAll": False,
                    "suppressMiniFilter": False,
                },
            )

    gb.configure_pagination(
        paginationAutoPageSize=False,
        paginationPageSize=int(
            rows_per_page
        ),
    )

    gb.configure_grid_options(
        suppressRowClickSelection=True,
        animateRows=False,
        enableCellTextSelection=True,
        ensureDomOrder=True,
        sideBar={
            "toolPanels": [
                {
                    "id": "filters",
                    "labelDefault": "Filters",
                    "labelKey": "filters",
                    "iconKey": "filter",
                    "toolPanel": "agFiltersToolPanel",
                },
                {
                    "id": "columns",
                    "labelDefault": "Columns",
                    "labelKey": "columns",
                    "iconKey": "columns",
                    "toolPanel": "agColumnsToolPanel",
                },
            ],
            "defaultToolPanel": "",
        },
    )

    grid_options = gb.build()

    grid_response = AgGrid(
        case_details,
        gridOptions=grid_options,
        update_mode=GridUpdateMode.FILTERING_CHANGED,
        data_return_mode=DataReturnMode.FILTERED,
        fit_columns_on_grid_load=False,
        allow_unsafe_jscode=False,
        enable_enterprise_modules=True,
        theme="streamlit",
        height=560,
        key=(
            f"attempt_excel_grid_"
            f"{clicked_attempt}_"
            f"{re.sub(r'[^A-Za-z0-9]+', '_', str(clicked_provider))}"
        ),
    )

    # --------------------------------------------------------
    # FILTERED DATA — ALL FILTERED ROWS, NOT CURRENT PAGE ONLY
    # --------------------------------------------------------
    filtered_grid_data = grid_response.get(
        "data",
        case_details,
    )

    if filtered_grid_data is None:
        filtered_grid_data = case_details.copy()

    if not isinstance(
        filtered_grid_data,
        pd.DataFrame,
    ):
        filtered_grid_data = pd.DataFrame(
            filtered_grid_data
        )

    filtered_count = int(
        len(filtered_grid_data)
    )

    st.markdown(
        f"**Showing {filtered_count:,} of "
        f"{clicked_total:,} records after filters**"
    )


    # --------------------------------------------------------
    # Download ALL filtered rows.
    # --------------------------------------------------------
    d1, d2 = st.columns(
        [1.6, 4.4]
    )

    with d1:
        safe_provider_name = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            str(clicked_provider),
        ).strip("_")

        st.download_button(
            "⬇ Download Filtered Data",
            data=filtered_grid_data.to_csv(
                index=False
            ).encode("utf-8-sig"),
            file_name=(
                f"credit_attempt_"
                f"{clicked_attempt}_"
                f"{safe_provider_name}_"
                f"filtered.csv"
            ),
            mime="text/csv",
            use_container_width=True,
            key=(
                f"aggrid_attempt_download_"
                f"{clicked_attempt}_"
                f"{safe_provider_name}"
            ),
        )



# ------------------------------------------------------------
# TABLE-5 POPUP ROUTING — NATIVE STREAMLIT STATE
# ------------------------------------------------------------
_popup_request = st.session_state.get(
    "credit_attempt_popup_request"
)

if _popup_request:
    _request_signature = _popup_request.get(
        "filter_signature"
    )

    _request_attempt_region = _popup_request.get(
        "attempt_region"
    )

    # The request is valid ONLY for the exact current top-filter state
    # and exact local Table-5 Region filter.
    if (
        _request_signature == _dashboard_filter_signature
        and _request_attempt_region == attempt_region_filter
    ):
        _clicked_attempt = int(
            _popup_request["attempt"]
        )

        _clicked_provider = str(
            _popup_request["provider"]
        )

        # This source is the CURRENT Table-5 population after all active
        # top filters + the local Analysis-5 Region filter.
        _clicked_cell = attempt_table_source[
            attempt_table_source["Attempt"].eq(
                _clicked_attempt
            )
            & attempt_table_source["Provider"].eq(
                _clicked_provider
            )
        ].copy()

        if not _clicked_cell.empty:
            show_attempt_case_dialog(
                _clicked_attempt,
                _clicked_provider,
                _clicked_cell,
                period_login_cases,
            )
        else:
            st.session_state.pop(
                "credit_attempt_popup_request",
                None,
            )

    else:
        # Any dashboard filter change invalidates the old drill-down.
        # Do NOT open anything automatically.
        st.session_state.pop(
            "credit_attempt_popup_request",
            None,
        )



# ============================================================
# 46B. TABLE 7 — FULL A1→A5 COMPACT CREDIT JOURNEY — STREAM 5.1 ROBUST POPUP
# ============================================================
# Visual rules:
# - ALL A1 lenders are shown.
# - Every recorded A2, A3, A4 and A5 lender is also shown.
# - Each lender card contains its own Approved / Pending / Rejected counts.
# - Only Rejected cases continue to the next attempt.
# - Later attempts are rendered as compact ROWS, not recursive branches.
#   This keeps every attempt visible without overlap or microscopic spaghetti.
# - The exact case-level routing is preserved.

st.markdown(r"""
<style>
/* STREAM 3.8 — FINAL HEADING SPACING / ANTI-OVERLAP PASS */
/* Give every major analytics heading its own vertical lane. */
.main div[data-testid="stMarkdownContainer"] h3 {
    margin-top: 18px !important;
    margin-bottom: 10px !important;
    padding-top: 2px !important;
    padding-bottom: 3px !important;
    line-height: 1.20 !important;
    min-height: 38px !important;
    position: relative !important;
    z-index: 3 !important;
}
/* Captions must sit below headings, never behind them. */
.main div[data-testid="stCaptionContainer"] {
    margin-top: 2px !important;
    margin-bottom: 10px !important;
    line-height: 1.35 !important;
    position: relative !important;
    z-index: 2 !important;
}
/* Charts/tables start after the title lane. */
.main [data-testid="stPlotlyChart"],
.main [data-testid="stDataFrame"],
.main iframe {
    position: relative !important;
    z-index: 1 !important;
}
/* Table 7 specifically needs extra separation because it follows Table 5. */
#table7-spacing-anchor { height: 8px; margin: 0; padding: 0; }
div[data-testid="stElementContainer"]:has(#table7-spacing-anchor) {
    margin-top: 10px !important;
    margin-bottom: 2px !important;
}
/* Table 5 header/filter row: absolute white, no legacy blue/navy glass styling. */
div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    background-image: none !important;
    border: 0 !important;
    box-shadow: none !important;
    padding: 4px 0 8px 0 !important;
}
div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) > div {
    background: #FFFFFF !important;
    background-image: none !important;
    border: 0 !important;
    box-shadow: none !important;
}
</style>
<div id="table7-spacing-anchor"></div>
""", unsafe_allow_html=True)

st.markdown('<div id="table7"></div>', unsafe_allow_html=True)
st.markdown("### 9. Credit Journey — Lender Recovery Path (A1 → A5)")
st.caption(
    "Every recorded lender attempt is shown. Each lender carries its own Approved / Pending / Rejected outcome; "
    "only rejected cases flow to the next attempt."
)

T7_REMARK_MAP = {'1 + dpds in two wheeler loan': ('High DPD', 'Credit Profile'),
 '1+ dpd with overdue due amount greater than norms| auto rejection! dpd > 30': ('Multiple Credit Issues',
                                                                                 'Credit Profile'),
 '1+ dpd with overdue due amount greater than norms| auto rejection! internal credit score lower than prescribed level': ('Multiple '
                                                                                                                          'Credit '
                                                                                                                          'Issues',
                                                                                                                          'Credit '
                                                                                                                          'Profile'),
 '1+ dpd with overdue due amount greater than norms| auto rejection! internal credit score lower than prescribed level, i don’t get salary in a bank account| rejected at app_susp_to_rejection': ('Bank '
                                                                                                                                                                                                   'Issue',
                                                                                                                                                                                                   'Banking'),
 '1+ dpds in gold loan': ('High DPD', 'Credit Profile'),
 '1+ dpds in hl': ('High DPD', 'Credit Profile'),
 '1+ dpds in mudra loan': ('High DPD', 'Credit Profile'),
 '1+ dpds in pl': ('High DPD', 'Credit Profile'),
 '1+ dpds in two wheeler loan': ('High DPD', 'Credit Profile'),
 '120+ dpds in gold loan': ('High DPD', 'Credit Profile'),
 '180+ dpds': ('High DPD', 'Credit Profile'),
 '180+ dpds in kisan credit card': ('High DPD', 'Credit Profile'),
 '30+ dpds in consumer loan': ('High DPD', 'Credit Profile'),
 '30+ dpds in credit card': ('High DPD', 'Credit Profile'),
 '30+ dpds in housing loan': ('High DPD', 'Credit Profile'),
 '30+ dpds in pl': ('High DPD', 'Credit Profile'),
 '470 credit card overdue': ('WOF/Default', 'Credit Profile'),
 '497 cibil & 180+dpds in business loan': ('Multiple Credit Issues', 'Credit Profile'),
 '534 cibil multiple dpds in gl , pl and etc': ('Multiple Credit Issues', 'Credit Profile'),
 '537 cibil 700 dpds in pl': ('Multiple Credit Issues', 'Credit Profile'),
 '540+ dpds in kisan credit card cibil 760': ('Multiple Credit Issues', 'Credit Profile'),
 '555 cibil & wof': ('Multiple Credit Issues', 'Credit Profile'),
 '60% approval moved to solfin': ('Moved Solfin', 'Lender Movement'),
 '60% approved': ('Partial Approval', 'Approval'),
 '60+ dpds': ('High DPD', 'Credit Profile'),
 '60+ dpds in 2 wheeler loan': ('High DPD', 'Credit Profile'),
 '60+ dpds in bl': ('High DPD', 'Credit Profile'),
 '60+ dpds in credit card': ('High DPD', 'Credit Profile'),
 '60+ dpds in gold loan': ('High DPD', 'Credit Profile'),
 '60+ dpds in kissan credit card': ('High DPD', 'Credit Profile'),
 '614 cibil & written off in personal loan': ('Multiple Credit Issues', 'Credit Profile'),
 '659 cibil and 60+ dpds in pl': ('Multiple Credit Issues', 'Credit Profile'),
 '670 cibil,overdue in active loan continuous dpd,cannot do': ('Multiple Credit Issues', 'Credit Profile'),
 '673 cibil , 60+ dpds in housing loan': ('Multiple Credit Issues', 'Credit Profile'),
 '720+ dpds': ('High DPD', 'Credit Profile'),
 '740 cibil recent cheque bounces': ('Low CIBIL', 'Credit Profile'),
 '90+ dpds': ('High DPD', 'Credit Profile'),
 '900+ dpds in two wheeler loan': ('High DPD', 'Credit Profile'),
 'abb is low': ('Low ABB', 'Credit Profile'),
 'abb low': ('Low ABB', 'Credit Profile'),
 'age is 60+': ('Age Issue', 'Policy'),
 'all nbfcs rejected': ('Credit Reject', 'Credit Decision'),
 'already applied before| dedupe rule rejection': ('Duplicate', 'Lead Quality'),
 'applicant cibil 490': ('Low CIBIL', 'Credit Profile'),
 'as per sales team confirmation project cancelled': ('Project Cancelled', 'Customer/Project'),
 'asking noc of existing loan in bank & moved to ecofy': ('Moved Ecofy', 'Lender Movement'),
 'auto rejection! minimum age norms not met': ('Age Issue', 'Policy'),
 'bank details not given and sales team not confrimed': ('Sales Pending', 'Internal'),
 'bank statement pending': ('Docs Pending', 'Documentation'),
 'bank statement uploaded, abb is low': ('Docs Pending', 'Documentation'),
 'bill commercial customer intrestred nps only after bill updation will do login': ('Moved NPS',
                                                                                    'Lender Movement'),
 'bureau score less than policy criteria| auto rejection - low bureau score': ('Low CIBIL', 'Credit Profile'),
 'bureau score less than policy criteria| auto rejection! internal credit score lower than prescribed level': ('Low '
                                                                                                               'CIBIL',
                                                                                                               'Credit '
                                                                                                               'Profile'),
 'case approved in solfin for ₹2,10,000, customer requires ₹1,500–₹2,000 emi case moved to nps as per vendor & sales team.': ('Moved '
                                                                                                                              'Solfin',
                                                                                                                              'Lender '
                                                                                                                              'Movement'),
 'case due to low cibil and dpd and overdue amount 36,435': ('Multiple Credit Issues', 'Credit Profile'),
 'case is approved in credit fair but with increased 1% of roi & customer is denied. case is moved to ecofy': ('Moved '
                                                                                                               'Ecofy',
                                                                                                               'Lender '
                                                                                                               'Movement'),
 'case is rejected due to low cibil score and settlement in credit card and overdue in credit cards': ('WOF/Default',
                                                                                                       'Credit '
                                                                                                       'Profile'),
 'case suspended due to aqb is lower than the internal limit': ('Low ABB', 'Credit Profile'),
 'cibil 314': ('Low CIBIL', 'Credit Profile'),
 'cibil 320': ('Low CIBIL', 'Credit Profile'),
 'cibil 400': ('Low CIBIL', 'Credit Profile'),
 'cibil 415': ('Low CIBIL', 'Credit Profile'),
 'cibil 437': ('Low CIBIL', 'Credit Profile'),
 'cibil 439': ('Low CIBIL', 'Credit Profile'),
 'cibil 444': ('Low CIBIL', 'Credit Profile'),
 'cibil 458 60+ dpds in mudra loan': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 468': ('Low CIBIL', 'Credit Profile'),
 'cibil 471': ('Low CIBIL', 'Credit Profile'),
 'cibil 472 720+dpds in consumer loan': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 483': ('Low CIBIL', 'Credit Profile'),
 'cibil 486': ('Low CIBIL', 'Credit Profile'),
 'cibil 487': ('Low CIBIL', 'Credit Profile'),
 'cibil 500': ('Low CIBIL', 'Credit Profile'),
 'cibil 503 dpds in kissan credit card': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 508 co applicant tractor loan suit file case not doable in credit fair': ('Multiple Credit Issues',
                                                                                  'Credit Profile'),
 'cibil 513': ('Low CIBIL', 'Credit Profile'),
 'cibil 514': ('Low CIBIL', 'Credit Profile'),
 'cibil 520': ('Low CIBIL', 'Credit Profile'),
 'cibil 566 720+ dpds in auto loan (personal)': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 603': ('Low CIBIL', 'Credit Profile'),
 'cibil 638 wof in auto loan': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 653': ('Low CIBIL', 'Credit Profile'),
 'cibil 655 , spm in kisan credit card.': ('Low CIBIL', 'Credit Profile'),
 'cibil 666 recent dpds overdues not doable': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 673 -kcc , housing loan emi overdue , multiple ecs bounce charges detected in banking , not doable': ('ROI/EMI '
                                                                                                              'Issue',
                                                                                                              'Customer/Project'),
 'cibil 691 60+ dpds in hl': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 693 - 120+dpds in used car loan': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 718 & need bank statement to proceed': ('Docs Pending', 'Documentation'),
 'cibil 722 30+ dpds in vechile loan': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 750 , wof in pl': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 756 dpds in od account moved to fibe': ('Moved FIBE', 'Lender Movement'),
 'cibil 758 30+ dpds auto loan (personal)': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 772 60+ dpds in hl': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 781 and wof in personal loan': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil 795 90+ dpds \\:two-wheeler loan': ('Multiple Credit Issues', 'Credit Profile'),
 'cibil below 650': ('Low CIBIL', 'Credit Profile'),
 'co app cibil 560': ('Low CIBIL', 'Credit Profile'),
 'co app dual pan': ('KYC/Mismatch', 'Documentation'),
 'co applicant cibil is low': ('Low CIBIL', 'Credit Profile'),
 'co- applicant cibil': ('Low CIBIL', 'Credit Profile'),
 'commercial': ('Commercial/Policy', 'Policy'),
 'commercial case': ('Commercial/Policy', 'Policy'),
 'commercial case moved to solfin': ('Moved Solfin', 'Lender Movement'),
 'confirmation pending from sales': ('Sales Pending', 'Internal'),
 'continuous dpd and overdue in active loans not doable,cibil low': ('Multiple Credit Issues',
                                                                     'Credit Profile'),
 'converted to cash': ('Cash', 'Payment/Conversion'),
 'credit card settelment': ('WOF/Default', 'Credit Profile'),
 'customer aadhar linked mobile number is not working, need to change mobile number. it will be hold till it done': ('Hold',
                                                                                                                     'Pending/Hold'),
 'customer address is not updated in electric bill': ('KYC/Mismatch', 'Documentation'),
 'customer already login with another vendor': ('Other Vendor', 'Lead Quality'),
 'customer bill address is not updated in discom': ('KYC/Mismatch', 'Documentation'),
 'customer bill address not updated in bill and pm surya ghar portal also customer need to updated in discom': ('KYC/Mismatch',
                                                                                                                'Documentation'),
 'customer call not responding past two days': ('Cust. NR', 'Customer/Project'),
 'customer confirmation pending': ('Cust. Pending', 'Customer/Project'),
 'customer converted ecofy': ('Moved Ecofy', 'Lender Movement'),
 'customer converted to cash': ('Cash', 'Payment/Conversion'),
 'customer converted to cash payment': ('Cash', 'Payment/Conversion'),
 'customer converted to ecofy': ('Moved Ecofy', 'Lender Movement'),
 'customer converted to solfin': ('Moved Solfin', 'Lender Movement'),
 'customer convertred to ecofy': ('Moved Ecofy', 'Lender Movement'),
 'customer does not have bank offer in his existing bank. need another bank account to proceed & informed to sales person': ('Bank '
                                                                                                                             'Issue',
                                                                                                                             'Banking'),
 'customer does not have loan offer in existing bank account. need another bank account to proceed': ('Bank '
                                                                                                      'Issue',
                                                                                                      'Banking'),
 'customer does not have loan offer in his existing bank.': ('Bank Issue', 'Banking'),
 'customer interested in fibe loan': ('Moved FIBE', 'Lender Movement'),
 'customer is not available as he went to vacation to other country': ('Cust. Unavailable',
                                                                       'Customer/Project'),
 'customer kyc surname and bill surname mismatched waiting for vendor conformation': ('KYC/Mismatch',
                                                                                      'Documentation'),
 'customer moved to fibe': ('Moved FIBE', 'Lender Movement'),
 'customer moved to nps': ('Moved NPS', 'Lender Movement'),
 'customer name is different in bank passbook & in other documents. name has to be correct in passbook': ('KYC/Mismatch',
                                                                                                          'Documentation'),
 'customer name mismatch in bank passbook & electricity bill. need to change': ('KYC/Mismatch',
                                                                                'Documentation'),
 'customer need time': ('Cust. Pending', 'Customer/Project'),
 'customer need to update address in ebill at discomm': ('KYC/Mismatch', 'Documentation'),
 'customer not answering': ('Cust. NR', 'Customer/Project'),
 'customer not answering call, confirmed by sales team.': ('Cust. NR', 'Customer/Project'),
 'customer not at confirmed to proceed with further': ('Cust. Pending', 'Customer/Project'),
 'customer not giving additional docs': ('Docs Pending', 'Documentation'),
 'customer not giving confirmation for login': ('Login/Processing', 'Processing'),
 'customer not interested': ('Not Interested', 'Customer/Project'),
 'customer not interested for multiple logins': ('Not Interested', 'Customer/Project'),
 'customer not responding': ('Cust. NR', 'Customer/Project'),
 'customer not supporting': ('Cust. Pending', 'Customer/Project'),
 'customer not supporting for login': ('Cust. Pending', 'Customer/Project'),
 'customer said wait for some time': ('Cust. Pending', 'Customer/Project'),
 'customer shifted solfin': ('Moved Solfin', 'Lender Movement'),
 'customer unable to provide statement': ('Docs Pending', 'Documentation'),
 'customer wants to nps': ('Moved NPS', 'Lender Movement'),
 'customer wants to proceed with nps.': ('Moved NPS', 'Lender Movement'),
 'customer wants to shift to nbfc': ('Moved NBFC', 'Lender Movement'),
 'cx confirmation pending': ('Cust. Pending', 'Customer/Project'),
 'cx not interested': ('Not Interested', 'Customer/Project'),
 'didnt get any confirmation from sales team': ('Sales Pending', 'Internal'),
 'didnt show bank offer to customer. so, moved to ecofy': ('Moved Ecofy', 'Lender Movement'),
 'disbursed in nps': ('Moved NPS', 'Lender Movement'),
 'dpds': ('High DPD', 'Credit Profile'),
 'dpds in bl': ('High DPD', 'Credit Profile'),
 'dpds in business loan': ('High DPD', 'Credit Profile'),
 'dpds in consumer loan': ('High DPD', 'Credit Profile'),
 'dpds in credit card': ('High DPD', 'Credit Profile'),
 'dpds in gold loan': ('High DPD', 'Credit Profile'),
 'dpds in kissan credit card': ('High DPD', 'Credit Profile'),
 'dpds in mudra loan': ('High DPD', 'Credit Profile'),
 'due to age moved to fibe': ('Moved FIBE', 'Lender Movement'),
 'due to low abb case rejected moved to nps': ('Moved NPS', 'Lender Movement'),
 "due to low abb need alternative banking and customer doesn't have alternative banking": ('Low ABB',
                                                                                           'Credit Profile'),
 'due to the roi, the customer is not interested in proceeding with the offer.': ('ROI Issue',
                                                                                  'Customer/Project'),
 'duplicate lead': ('Duplicate', 'Lead Quality'),
 'duplicate lead, confirmed by sales team': ('Duplicate', 'Lead Quality'),
 'ebill is on customer father name & documents are pending to proceed with login': ('Docs Pending',
                                                                                    'Documentation'),
 'ebill is on father name & need documents to proceed. want confirmation from customer & sales person': ('Docs '
                                                                                                         'Pending',
                                                                                                         'Documentation'),
 'ecofy': ('Moved Ecofy', 'Lender Movement'),
 'ecofy rejected and case approved in credit fair': ('Credit Reject', 'Credit Decision'),
 'fcu rejected': ('Credit Reject', 'Credit Decision'),
 'finance rejected loan loan1066': ('Credit Reject', 'Credit Decision'),
 'finance rejected loan loan934': ('Credit Reject', 'Credit Decision'),
 'finance rejected loan loan945': ('Credit Reject', 'Credit Decision'),
 'finance rejected loan loan983': ('Credit Reject', 'Credit Decision'),
 'glitch': ('Tech Issue', 'System'),
 'high consumption e bill required. moved to credit fair': ('Moved CF', 'Lender Movement'),
 'high dpd': ('High DPD', 'Credit Profile'),
 "high dpd's and overdue in active loans": ('Multiple Credit Issues', 'Credit Profile'),
 'high dpd+ low cibil': ('Multiple Credit Issues', 'Credit Profile'),
 'high dpds': ('High DPD', 'Credit Profile'),
 'high dpds & over dues in existing loans': ('High DPD', 'Credit Profile'),
 'highdpds': ('High DPD', 'Credit Profile'),
 'hold': ('Hold', 'Pending/Hold'),
 'hold case': ('Hold', 'Pending/Hold'),
 'incompleted docs': ('Docs Pending', 'Documentation'),
 'internal credit score below the prescribed level (bre2-a5) | auto-rejection! internal credit score is lower than the prescribed level': ('Credit '
                                                                                                                                           'Reject',
                                                                                                                                           'Credit '
                                                                                                                                           'Decision'),
 'internal credit score lower than prescribed level (bre2-a5)| auto rejection! internal credit score lower than prescribed level': ('Credit '
                                                                                                                                    'Reject',
                                                                                                                                    'Credit '
                                                                                                                                    'Decision'),
 'it is login with other vendor & need to be rejected to proceed with us': ('Other Vendor', 'Lead Quality'),
 "it's logged in other vendor": ('Other Vendor', 'Lead Quality'),
 'junk lead': ('Junk Lead', 'Lead Quality'),
 'just lead created': ('New Lead', 'Processing'),
 'kcc 90+ dpds': ('High DPD', 'Credit Profile'),
 'kcc dpd, low cibil': ('Multiple Credit Issues', 'Credit Profile'),
 'kisan credit card dpd is 60+ dpd': ('High DPD', 'Credit Profile'),
 'lead2719 disbursement done for another project customer need time,confirmed by sales team.': ('Cust. '
                                                                                                'Pending',
                                                                                                'Customer/Project'),
 'loan status changed for loan1149': ('Status Changed', 'Processing'),
 'loan status changed for loan1152': ('Status Changed', 'Processing'),
 'login': ('Login/Processing', 'Processing'),
 'login in credit fair': ('Moved CF', 'Lender Movement'),
 'login in ecofy': ('Moved Ecofy', 'Lender Movement'),
 'login in fibe': ('Moved FIBE', 'Lender Movement'),
 'login in nps': ('Moved NPS', 'Lender Movement'),
 'login on 25-8-2026': ('Login/Processing', 'Processing'),
 'low abb': ('Low ABB', 'Credit Profile'),
 'low abb - le rejected': ('Low ABB', 'Credit Profile'),
 'low abb and 60+ dpds': ('Multiple Credit Issues', 'Credit Profile'),
 'low abb and dpds in last 12 months': ('Multiple Credit Issues', 'Credit Profile'),
 'low abb and multiple dpds': ('Multiple Credit Issues', 'Credit Profile'),
 'low abb cibil score -3': ('Multiple Credit Issues', 'Credit Profile'),
 'low abb moved to nps': ('Moved NPS', 'Lender Movement'),
 'low abb need another statement': ('Low ABB', 'Credit Profile'),
 'low abbb': ('Low ABB', 'Credit Profile'),
 'low aqb': ('Low ABB', 'Credit Profile'),
 'low cibil': ('Low CIBIL', 'Credit Profile'),
 'low cibil & applies before': ('Low CIBIL', 'Credit Profile'),
 'low cibil 429': ('Low CIBIL', 'Credit Profile'),
 'low cibil 529': ('Low CIBIL', 'Credit Profile'),
 'low cibil and dpds in housing loan': ('Multiple Credit Issues', 'Credit Profile'),
 'low cibil and high dpds': ('Multiple Credit Issues', 'Credit Profile'),
 'madatampalli case bank manager not supporting': ('Age Issue', 'Policy'),
 'manual action : reject 10000000010334 same applicant already rejected same e-bill and address. (knockoff reject - with reason ovd)': ('Credit '
                                                                                                                                        'Reject',
                                                                                                                                        'Credit '
                                                                                                                                        'Decision'),
 'meter name change': ('KYC/Mismatch', 'Documentation'),
 'mostly gold loans and negative pincode , risky case , please share bank statement': ('Docs Pending',
                                                                                       'Documentation'),
 'move to solfin': ('Moved Solfin', 'Lender Movement'),
 'moved fibe': ('Moved FIBE', 'Lender Movement'),
 'moved solfin': ('Moved Solfin', 'Lender Movement'),
 'moved to bajaj': ('Moved Bajaj', 'Lender Movement'),
 'moved to ecofy': ('Moved Ecofy', 'Lender Movement'),
 'moved to fibe': ('Moved FIBE', 'Lender Movement'),
 'moved to fibe due to partially approved in fibe': ('Moved FIBE', 'Lender Movement'),
 'moved to nps': ('Moved NPS', 'Lender Movement'),
 'moved to solfin': ('Moved Solfin', 'Lender Movement'),
 'nach done in fibe': ('NACH Done', 'Processing'),
 'name mismatched with bill and bank passbook after bill updating name we can do': ('KYC/Mismatch',
                                                                                    'Documentation'),
 'need a active & clear ifsc code to proceed with login': ('Bank Issue', 'Banking'),
 'need co app documents': ('Docs Pending', 'Documentation'),
 'new lead2796': ('New Lead', 'Processing'),
 'no bank loan offer in customer existing bank. moved to ecofy': ('Moved Ecofy', 'Lender Movement'),
 'not confirmed': ('Pending Confirmation', 'Pending/Hold'),
 'not interested': ('Not Interested', 'Customer/Project'),
 'not yet received signed franking documents & no confirmation regarding this from customer': ('Docs Pending',
                                                                                               'Documentation'),
 'nps': ('Moved NPS', 'Lender Movement'),
 'partial approval': ('Partial Approval', 'Approval'),
 'partial approved': ('Partial Approval', 'Approval'),
 'partial approved moved to credit fair': ('Moved CF', 'Lender Movement'),
 'partially approved , moved to solfin': ('Moved Solfin', 'Lender Movement'),
 'pending at sales end': ('Sales Pending', 'Internal'),
 'pending at sales end. not provided bank details to proceed': ('Sales Pending', 'Internal'),
 'pending at sales end. not submitted signed agreement documents': ('Sales Pending', 'Internal'),
 'pending from sales end': ('Sales Pending', 'Internal'),
 'present bank account is not active & need alternative bank account to proceed': ('Bank Issue', 'Banking'),
 'price not confrimed': ('Sales Pending', 'Internal'),
 'project cancelled': ('Project Cancelled', 'Customer/Project'),
 'reason: reject this case due to income is low and abb is low and multiple dpd and overdue amount. remarks: reject this case due to income is low and abb is low and multiple dpd and overdue amount': ('Multiple '
                                                                                                                                                                                                         'Credit '
                                                                                                                                                                                                         'Issues',
                                                                                                                                                                                                         'Credit '
                                                                                                                                                                                                         'Profile'),
 'registered with another vendor': ('Other Vendor', 'Lead Quality'),
 'registered with other vendor': ('Other Vendor', 'Lead Quality'),
 'rejected as customer is having large check bounces': ('WOF/Default', 'Credit Profile'),
 'rejected cx not interested': ('Not Interested', 'Customer/Project'),
 'rejected due to 60+ dpd in last 12 months in multiple loans + cibil score is low + default outstanding above 1 lakh + co applicant is also doing defaults + abb is (1)rs + banking is almost nil': ('Multiple '
                                                                                                                                                                                                      'Credit '
                                                                                                                                                                                                      'Issues',
                                                                                                                                                                                                      'Credit '
                                                                                                                                                                                                      'Profile'),
 'rejected due to 90+ dpd in last 12 months in multiple loans + default outstanding above 5 lakh + abb is low + overall banking is nil.': ('Multiple '
                                                                                                                                           'Credit '
                                                                                                                                           'Issues',
                                                                                                                                           'Credit '
                                                                                                                                           'Profile'),
 'rejected due to 900+ dpd in last 12 months + abb is too low 1k + marginal income': ('Multiple Credit '
                                                                                      'Issues',
                                                                                      'Credit Profile'),
 'rejected due to cibil score is low + default outstanding 28k + recent 60+ dpd in loans + abb is low.': ('Multiple '
                                                                                                          'Credit '
                                                                                                          'Issues',
                                                                                                          'Credit '
                                                                                                          'Profile'),
 'rejected due to negative profile.': ('Credit Reject', 'Credit Decision'),
 'rejected due to ntc case + income is low + abb is low': ('Low ABB', 'Credit Profile'),
 'rejected due to ntc case + tier 3 + low abb + required % risk score criteria not met.': ('Low ABB',
                                                                                           'Credit Profile'),
 'rejected in all': ('Credit Reject', 'Credit Decision'),
 'rejected login done in fibe': ('Moved FIBE', 'Lender Movement'),
 'rejected nps': ('Credit Reject', 'Credit Decision'),
 'relook': ('Relook', 'Processing'),
 'required documents are pending at customer end': ('Docs Pending', 'Documentation'),
 'salary amount less than internal limit| auto rejection! internal credit score lower than prescribed level': ('Credit '
                                                                                                               'Reject',
                                                                                                               'Credit '
                                                                                                               'Decision'),
 'sales not confirmed': ('Pending Confirmation', 'Pending/Hold'),
 'sales team not confirmed': ('Pending Confirmation', 'Pending/Hold'),
 'sales team not confirmed loan or cash': ('Pending Confirmation', 'Pending/Hold'),
 'sales team not confirming': ('Sales Pending', 'Internal'),
 'shifted to nps': ('Moved NPS', 'Lender Movement'),
 'statement is not enough & low abb. moved to nps': ('Moved NPS', 'Lender Movement'),
 'statement pending': ('Docs Pending', 'Documentation'),
 'suit filed in tractor loan': ('WOF/Default', 'Credit Profile'),
 'system rejected in sbi branch in hi-tech branch': ('Credit Reject', 'Credit Decision'),
 'technical glitch': ('Tech Issue', 'System'),
 'temple cannot be process for loan': ('Commercial/Policy', 'Policy'),
 'the applicant had one previous loan, which also had defaults + tier 3 case + banking is almost nill overall case rejected': ('Multiple '
                                                                                                                               'Credit '
                                                                                                                               'Issues',
                                                                                                                               'Credit '
                                                                                                                               'Profile'),
 'the applicant’s cibil report shows a current overdue amount of ₹17,407.00. additionally, a recent 60+ dpd has been reported within the last 12 months.+ tier 3 case + risk score matrix not met overall case rejected': ('Multiple '
                                                                                                                                                                                                                           'Credit '
                                                                                                                                                                                                                           'Issues',
                                                                                                                                                                                                                           'Credit '
                                                                                                                                                                                                                           'Profile'),
 'the applicant’s cibil report shows a current overdue amount of ₹31,340.00, along with a recent 60+ dpd. overall case rejected': ('Multiple '
                                                                                                                                   'Credit '
                                                                                                                                   'Issues',
                                                                                                                                   'Credit '
                                                                                                                                   'Profile'),
 'the applicant’s cibil report shows a current overdue amount of ₹35,322.00. additionally, a recent 60+ dpd has been reported in the housing loan within the last 12 months.overall case rejected': ('Multiple '
                                                                                                                                                                                                     'Credit '
                                                                                                                                                                                                     'Issues',
                                                                                                                                                                                                     'Credit '
                                                                                                                                                                                                     'Profile'),
 "the case has been rejected because the applicant has a cibil default with a credit score of 661 and recent dpds in the personal loan along with an overdue balance of ₹17,600 + the applicant's banking activity is low, with an average bank balance of ₹1,755. hence case rejected": ('Age '
                                                                                                                                                                                                                                                                                          'Issue',
                                                                                                                                                                                                                                                                                          'Policy'),
 "the case has been rejected because the electricity bill amount is low at ₹72, and the proposed emi is high + the applicant has recent 60+ dpds in the two-wheeler loan during the last 12 months + the applicant's income is marginal, hence case rejected.": ('ROI/EMI '
                                                                                                                                                                                                                                                                 'Issue',
                                                                                                                                                                                                                                                                 'Customer/Project'),
 'the case is rejected due to cibil,score is low and dpd in last 12 month secure loan.': ('Multiple Credit '
                                                                                          'Issues',
                                                                                          'Credit Profile'),
 'the current banking abb is almost nil.': ('Low ABB', 'Credit Profile'),
 'there was no proper confirmation from the key accounts team. initially, the loan tenure was 48 months, and later it was changed to 60 months. due to this lack of clear communication, customers were processed with incorrect information, leading to confusion and cancellations': ('Project '
                                                                                                                                                                                                                                                                                        'Cancelled',
                                                                                                                                                                                                                                                                                        'Customer/Project'),
 'this case due to cibil is low and multiple loans dpd and overdue amount 41,520': ('Multiple Credit Issues',
                                                                                    'Credit Profile'),
 'this lead duplicate lead2493 sales team created same customer with another lead lead2619': ('Duplicate',
                                                                                              'Lead Quality'),
 'vendor change': ('Vendor Change', 'Lead Quality'),
 'wof in consumer loan': ('WOF/Default', 'Credit Profile'),
 'wof in pl': ('WOF/Default', 'Credit Profile'),
 'wof in pl 650 cibil': ('Multiple Credit Issues', 'Credit Profile'),
 'wof in two wheeler loan': ('WOF/Default', 'Credit Profile')}

def _t7_norm_remark(value):
    return re.sub(r"\s+", " ", clean_text(value).casefold()).strip()

def _t7_map_remark(value):
    raw = clean_text(value)
    if not raw:
        return ("No Remark", "Unmapped")
    mapped = T7_REMARK_MAP.get(_t7_norm_remark(raw))
    return mapped if mapped else (raw[:90], "Unmapped")

def _t7_bucket(stage, substage):
    out = classify_attempt_outcome(stage, substage)
    if out in {"Approved", "Rejected", "Login Done", "Documents Submitted"}:
        return out
    return "Other / Review"

def _t7_pct(n, d):
    return (float(n) / float(d) * 100.0) if d else 0.0

def _t7_esc(v):
    return html.escape(clean_text(v))

t7 = attempt_table_source.copy()

if t7.empty:
    st.info("No attempt journey is available for Table 7 under the current filters.")
else:
    t7["_T7_Attempt"] = pd.to_numeric(t7["Attempt"], errors="coerce")
    t7["_T7_Provider"] = t7["Provider"].apply(clean_text).apply(normalize_provider)
    t7["_T7_Outcome"] = t7.apply(
        lambda r: _t7_bucket(r.get("Stage", ""), r.get("Substage", "")), axis=1
    )
    t7 = t7.loc[
        t7["_T7_Attempt"].notna()
        & t7["_T7_Provider"].ne("")
        & t7["_CaseIndex"].notna()
    ].copy()
    t7["_T7_Attempt"] = t7["_T7_Attempt"].astype(int)

    if "Comments" in period_login_cases.columns:
        _comment_lookup = period_login_cases["Comments"].to_dict()
        t7["_T7_Comment"] = t7["_CaseIndex"].map(_comment_lookup).fillna("").astype(str)
    else:
        t7["_T7_Comment"] = ""

    _mapped = t7["_T7_Comment"].apply(_t7_map_remark)
    t7["_T7_ShortRemark"] = _mapped.apply(lambda x: x[0])
    t7["_T7_RemarkGroup"] = _mapped.apply(lambda x: x[1])

    t7 = (
        t7.sort_values(["_CaseIndex", "_T7_Attempt", "Attempt Date"])
        .drop_duplicates(["_CaseIndex", "_T7_Attempt"], keep="first")
        .copy()
    )

    a1 = t7.loc[t7["_T7_Attempt"].eq(1)].copy()
    total_cases = int(a1["_CaseIndex"].nunique())

    latest_t7 = (
        t7.sort_values(["_CaseIndex", "_T7_Attempt"])
        .drop_duplicates("_CaseIndex", keep="last")
    )
    approved_any_ids = set(t7.loc[t7["_T7_Outcome"].eq("Approved"), "_CaseIndex"])
    rejected_any_ids = set(t7.loc[t7["_T7_Outcome"].eq("Rejected"), "_CaseIndex"])
    pending_latest_ids = set(
        latest_t7.loc[
            latest_t7["_T7_Outcome"].isin(["Login Done", "Documents Submitted", "Other / Review"]),
            "_CaseIndex",
        ]
    )
    avg_attempts = float(t7.groupby("_CaseIndex")["_T7_Attempt"].max().mean()) if not t7.empty else 0.0

    # ---------- KPI strip ----------
    st.markdown("""
    <style>
    .cj-kpi{border-radius:11px;padding:11px 14px;color:#fff;min-height:72px;
    box-shadow:0 4px 13px rgba(25,55,82,.10)}
    .cj-kpi small{font-size:10px;font-weight:800;opacity:.92}
    .cj-kpi strong{display:block;font-size:26px;line-height:1.05;margin:4px 0 2px}
    .cj-kpi span{font-size:10px;font-weight:700;opacity:.92}
    .cj-blue{background:linear-gradient(135deg,#173E70,#0E5B91)}
    .cj-green{background:linear-gradient(135deg,#1B9563,#31B77A)}
    .cj-amber{background:linear-gradient(135deg,#DE9500,#F3B51B)}
    .cj-red{background:linear-gradient(135deg,#C33A49,#E45D5D)}
    .cj-purple{background:linear-gradient(135deg,#5F4FD1,#7B62E6)}
    </style>
    """, unsafe_allow_html=True)

    k1,k2,k3,k4,k5 = st.columns(5)
    for col, cls, label, value, sub in [
        (k1,"cj-blue","TOTAL CASES",total_cases,"Filtered journey"),
        (k2,"cj-green","TOTAL APPROVED",len(approved_any_ids),f"{_t7_pct(len(approved_any_ids),total_cases):.1f}%"),
        (k3,"cj-amber","TOTAL PENDING",len(pending_latest_ids),f"{_t7_pct(len(pending_latest_ids),total_cases):.1f}% latest state"),
        (k4,"cj-red","TOTAL REJECTED",len(rejected_any_ids),f"{_t7_pct(len(rejected_any_ids),total_cases):.1f}% rejected somewhere"),
        (k5,"cj-purple","AVG. ATTEMPTS / CASE",f"{avg_attempts:.1f}","Recorded attempts"),
    ]:
        with col:
            shown = value if isinstance(value, str) else f"{value:,}"
            st.markdown(
                f'<div class="cj-kpi {cls}"><small>{label}</small>'
                f'<strong>{shown}</strong><span>{sub}</span></div>',
                unsafe_allow_html=True,
            )

    # ---------- Build exact route cohorts ----------
    # Each route node is identified by its complete path:
    # (A1 lender, A2 lender, ... current lender)
    route_nodes = {}
    for cid, grp in t7.groupby("_CaseIndex", sort=False):
        rows = grp.sort_values("_T7_Attempt")
        path = []
        previous_rejected = True
        for _, rr in rows.iterrows():
            att = int(rr["_T7_Attempt"])
            if att < 1 or att > 5:
                continue
            # A2+ is only a valid continuation when the preceding recorded attempt rejected.
            if att > 1 and not previous_rejected:
                break
            provider = rr["_T7_Provider"]
            path.append(provider)
            key = tuple(path)
            node = route_nodes.setdefault(key, {
                "attempt": att, "provider": provider, "cases": set(),
                "approved": set(), "pending": set(), "rejected": set(),
                "login": set(), "docs": set(), "other": set(),
                "reason_counts": {}
            })
            node["cases"].add(cid)
            outcome = rr["_T7_Outcome"]
            if outcome == "Approved":
                node["approved"].add(cid)
            elif outcome == "Rejected":
                node["rejected"].add(cid)
                reason = clean_text(rr["_T7_ShortRemark"]) or "No Remark"
                node["reason_counts"].setdefault(reason, set()).add(cid)
            else:
                node["pending"].add(cid)
                if outcome == "Login Done":
                    node["login"].add(cid)
                elif outcome == "Documents Submitted":
                    node["docs"].add(cid)
                else:
                    node["other"].add(cid)
            previous_rejected = (outcome == "Rejected")

    nodes_by_attempt = {
        att: sorted(
            [(path, n) for path, n in route_nodes.items() if n["attempt"] == att],
            key=lambda z: (-len(z[1]["cases"]), " → ".join(z[0]))
        )
        for att in range(1, 6)
    }

    # ---------- TABLE 7 CLICKABLE NODE POPUP ----------
    # Each A1→A5 lender card receives a deterministic node id.  The HTML tree
    # uses a normal top-level query-string link because components.html lives
    # inside an iframe.  We consume the query parameter BEFORE opening the
    # dialog, so Streamlit's native top-right X closes cleanly and the old
    # card is not left selected / reopened on the next interaction.
    _t7_sorted_routes = sorted(
        route_nodes.keys(),
        key=lambda r: (len(r), " → ".join(str(x) for x in r))
    )
    _t7_node_id_to_route = {
        f"n{i+1}": route for i, route in enumerate(_t7_sorted_routes)
    }
    _t7_route_to_node_id = {
        route: node_id for node_id, route in _t7_node_id_to_route.items()
    }

    @st.dialog("Credit Journey Case Details", width="large")
    def _show_t7_node_dialog(_route, _node):
        _att = int(_node["attempt"])
        _provider = clean_text(_node["provider"])
        _ids = list(_node["cases"])
        _total = len(_ids)
        _approved = len(_node["approved"])
        _pending = len(_node["pending"])
        _rejected = len(_node["rejected"])

        st.markdown(f"### A{_att} · {_provider}")
        st.caption(" → ".join(clean_text(x) for x in _route))

        _c1, _c2, _c3, _c4 = st.columns(4)
        _c1.metric("Cases", f"{_total:,}")
        _c2.metric("Approved", f"{_approved:,}")
        _c3.metric("Pending", f"{_pending:,}")
        _c4.metric("Rejected", f"{_rejected:,}")

        # Exact attempt rows represented by this node.
        _attempt_rows = t7.loc[
            t7["_CaseIndex"].isin(_ids)
            & t7["_T7_Attempt"].eq(_att)
            & t7["_T7_Provider"].eq(_provider)
        ].copy()

        # Recover the full case fields from the dashboard source, then attach
        # the clicked attempt's lender/outcome information.
        _case_rows = period_login_cases.loc[
            period_login_cases.index.isin(_ids)
        ].copy()
        _case_rows["_CaseIndex"] = _case_rows.index

        _attempt_keep = [
            c for c in [
                "_CaseIndex", "_T7_Attempt", "_T7_Provider", "_T7_Outcome",
                "Stage", "Substage", "Attempt Date", "_T7_ShortRemark"
            ] if c in _attempt_rows.columns
        ]
        if _attempt_keep:
            _attempt_view = _attempt_rows[_attempt_keep].drop_duplicates("_CaseIndex")
            _case_rows = _case_rows.merge(_attempt_view, on="_CaseIndex", how="left")

        _preferred = [
            "LEAD ID", "Region", "_T7_Provider", "Customer Name", "Current Status",
            "_T7_Outcome", "Stage", "Substage", "Loan Sub Stage", "Login Done On",
            "Attempt Date", "Disbursed At", "TAT Days", "Project Value",
            "Disbursed Amount", "_T7_ShortRemark"
        ]
        _cols = [c for c in _preferred if c in _case_rows.columns]
        if not _cols:
            _cols = [c for c in _case_rows.columns if not c.startswith("_")][:18]

        _display = _case_rows[_cols].copy()
        _rename = {
            "_T7_Provider": "Bank/NBFC",
            "_T7_Outcome": "Attempt Outcome",
            "_T7_ShortRemark": "Rejection Reason",
        }
        _display = _display.rename(columns=_rename)

        # Make financial values presentation-friendly while preserving the
        # underlying data in the CSV download.
        for _money_col in ["Project Value", "Disbursed Amount"]:
            if _money_col in _display.columns:
                _display[_money_col] = pd.to_numeric(
                    _display[_money_col], errors="coerce"
                ).round(2)

        st.dataframe(
            _display,
            use_container_width=True,
            hide_index=True,
            height=min(520, 58 + max(len(_display), 1) * 38),
        )

        st.download_button(
            "Download cases",
            data=_display.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"Table7_A{_att}_{_provider}_cases.csv".replace(" ", "_"),
            mime="text/csv",
            use_container_width=True,
            key=f"t7_download_{_att}_{abs(hash(_route))}",
        )

    # TABLE 7 click transport is handled by a Streamlit Components V2 trigger below.
    # No query parameters, browser navigation, forced st.rerun(), or page jump are used.

    # ---------- FINAL HORIZONTAL FAMILY TREE: FULL CARDS A1 → A5 ----------
    # No wrapping, no squeezing, no bank limit.
    # Every route node uses the same readable card.
    # The canvas grows horizontally and the user scrolls right.

    def _tree_reason_label(value):
        raw = clean_text(value)
        if not raw:
            return "No Remark"
        s = re.sub(r"\s+", " ", raw).strip()
        low = s.casefold()

        if "down payment" in low and ("rengy" in low or "our company" in low):
            return "Down Payment by Rengy"
        if "doc" in low and ("pending" in low or "document" in low):
            return "Docs Pending"
        if "low cibil" in low or ("cibil" in low and "low" in low):
            return "Low CIBIL"
        if "dpd" in low:
            return "High DPDs"
        if "foir" in low:
            return "High FOIR"
        if "income" in low and ("low" in low or "salary" in low):
            return "Income Low"
        if "not available" in low or "unavailable" in low:
            return "Customer Unavailable"
        if "move" in low and "solfin" in low:
            return "Moved to Solfin"
        if "app detail" in low:
            return "App Details Needed"
        if low in {"no remark", "no remarks", "na", "n/a", "none", "-"}:
            return "No Remark"
        if len(s) > 44:
            return "Other"
        return s[:44]

    # Display-only cleanup of rejection remarks.
    _tree_case_reason = {}
    if "_T7_ShortRemark" in t7.columns:
        for _, _rr in t7.iterrows():
            _tree_case_reason[_rr["_CaseIndex"]] = _tree_reason_label(_rr["_T7_ShortRemark"])

    for _route, _node in route_nodes.items():
        _clean = {}
        for _cid in _node["rejected"]:
            _reason = _tree_case_reason.get(_cid, "No Remark")
            _clean.setdefault(_reason, set()).add(_cid)
        _node["_tree_reasons"] = _clean

    def _tree_children(route):
        target = len(route) + 1
        return sorted(
            [(r, n) for r, n in route_nodes.items()
             if len(r) == target and r[:-1] == route],
            key=lambda z: (-len(z[1]["cases"]), z[1]["provider"])
        )

    def _tree_pct(n, d):
        return (float(n) / float(d) * 100.0) if d else 0.0

    def _tree_reason_rows(node, limit=4):
        vals = sorted(
            ((r, len(ids)) for r, ids in node.get("_tree_reasons", {}).items()),
            key=lambda z: (-z[1], z[0])
        )
        if not vals:
            return '<div class="empty">No rejected cases</div>'

        shown = vals[:limit]
        rest = vals[limit:]
        rows = "".join(
            f'<div class="mini-row"><span>{html.escape(clean_text(r))}</span><b>{n:,}</b></div>'
            for r, n in shown
        )
        if rest:
            rows += (
                f'<div class="mini-row other"><span>Other</span>'
                f'<b>{sum(n for _, n in rest):,}</b></div>'
            )
        return rows

    def _tree_card(route, node):
        att = node["attempt"]
        total = max(len(node["cases"]), 1)
        parent = route[-2] if len(route) > 1 else ""
        route_line = ""
        if att > 1:
            route_line = (
                f'<div class="route-line">'
                f'<span>FROM</span> {html.escape(clean_text(parent))} '
                f'<b>→</b> {html.escape(clean_text(node["provider"]))}'
                f'</div>'
            )

        app = len(node["approved"])
        pend = len(node["pending"])
        rej = len(node["rejected"])

        # Rejected routing reconciliation:
        # pushed = unique rejected cases that actually appear in immediate next-attempt child nodes.
        # left = rejected cases with no immediate next attempt recorded.
        _child_nodes = _tree_children(route) if att < 5 else []
        _pushed_ids = set()
        for _child_route, _child_node in _child_nodes:
            _pushed_ids.update(node["rejected"].intersection(_child_node["cases"]))
        pushed = len(_pushed_ids)
        left = max(rej - pushed, 0)

        routing_badge = ""
        if rej > 0:
            routing_badge = f"""
            <div class="routing-summary">
              <div class="routing-item pushed">
                <span>PUSHED</span><b>{pushed:,}</b>
              </div>
              <div class="routing-divider"></div>
              <div class="routing-item left">
                <span>LEFT</span><b>{left:,}</b>
              </div>
            </div>
            """

        _node_id = _t7_route_to_node_id.get(route, "")
        # Exact 5.1 card stays visually unchanged. The node id is now carried
        # as data only; Components V2 captures the click without navigation.
        return f"""
        <a class="t7-card-link" href="#" data-t7node="{html.escape(_node_id)}" title="Click for case details">
        <div class="bank-card a{att}">
          <div class="bank-head">
            <span class="attempt-badge">A{att}</span>
            <div class="bank-name">
              <b>{html.escape(clean_text(node["provider"]))}</b>
              <small>{len(node["cases"]):,} cases</small>
            </div>
            {routing_badge}
          </div>

          {route_line}

          <div class="status-grid">
            <div class="status app">
              <span>Approved</span><b>{app:,}</b><small>{_tree_pct(app,total):.1f}%</small>
            </div>
            <div class="status pend">
              <span>Pending</span><b>{pend:,}</b><small>{_tree_pct(pend,total):.1f}%</small>
            </div>
            <div class="status rej">
              <span>Rejected</span><b>{rej:,}</b><small>{_tree_pct(rej,total):.1f}%</small>
            </div>
          </div>

          <div class="detail-grid">
            <div class="detail pending-detail">
              <div class="detail-title">Pending Breakdown</div>
              <div class="mini-row"><span>Login Done</span><b>{len(node["login"]):,}</b></div>
              <div class="mini-row"><span>Docs Submitted</span><b>{len(node["docs"]):,}</b></div>
              <div class="mini-row"><span>Other Pending</span><b>{len(node["other"]):,}</b></div>
            </div>

            <div class="detail reject-detail">
              <div class="detail-title">Rejection Reasons</div>
              {_tree_reason_rows(node, 4)}
            </div>
          </div>
        </div>
        </a>
        """

    def _tree_branch(route, node):
        children = _tree_children(route)
        child_html = ""
        if children:
            child_html = (
                '<ul>'
                + "".join(
                    f'<li>{_tree_branch(child_route, child_node)}</li>'
                    for child_route, child_node in children
                )
                + '</ul>'
            )
        elif node["rejected"]:
            child_html = '<div class="terminal">No further recorded attempt</div>'

        return _tree_card(route, node) + child_html

    a1_nodes = nodes_by_attempt[1]

    tree_html = (
        '<ul class="tree-root">'
        + "".join(
            f'<li>{_tree_branch(route, node)}</li>'
            for route, node in a1_nodes
        )
        + '</ul>'
    )

    # Width is deliberately NOT constrained to the laptop viewport.
    # Each leaf gets enough real estate for a readable 300px card.
    leaf_count = sum(
        1 for route, node in route_nodes.items()
        if not _tree_children(route)
    )
    a1_count = max(len(a1_nodes), 1)
    canvas_width = max(
        1550,
        a1_count * 325,
        min(max(leaf_count, a1_count), 40) * 315
    )

    deepest_attempt = max(
        [n["attempt"] for n in route_nodes.values()] or [1]
    )
    component_height = 125 + deepest_attempt * 330

    board_html = f"""
    <!doctype html>
    <html>
    <head>
    <meta charset="utf-8">
    <style>
    *{{box-sizing:border-box}}
    html,body{{
      margin:0;
      background:#FFFFFF;
      color:#153451;
      font-family:Inter,Segoe UI,Arial,sans-serif;
    }}

    .viewport{{
      width:100%;
      overflow-x:scroll;
      overflow-y:hidden;
      overflow-anchor:none;
      background:#FFFFFF;
      border:1px solid #DDE8F1;
      border-radius:14px;
      padding:10px 0 18px;
      scrollbar-width:auto;
    }}

    .canvas{{
      display:inline-block;
      width:max-content;
      min-width:max(100%, {canvas_width}px);
      padding:0 60px 26px 80px;
      position:relative;
      vertical-align:top;
    }}

    .scroll-note{{
      position:sticky;
      left:14px;
      width:max-content;
      z-index:50;
      background:#E8F2FA;
      border:1px solid #C8DDEA;
      color:#315B78;
      border-radius:999px;
      padding:5px 10px;
      font-size:9px;
      font-weight:850;
      margin-bottom:8px;
    }}

    .root-wrap{{
      position:sticky;
      left:0;
      width:100vw;
      max-width:100vw;
      display:flex;
      justify-content:center;
      margin-bottom:2px;
      z-index:20;
      pointer-events:none;
    }}

    .root{{
      width:260px;
      background:linear-gradient(135deg,#174777,#0D6098);
      color:#fff;
      border-radius:10px;
      text-align:center;
      padding:9px 12px;
      box-shadow:0 5px 14px rgba(20,55,85,.14);
    }}
    .root small{{display:block;font-size:9px;font-weight:900;letter-spacing:.3px}}
    .root b{{display:block;font-size:26px;line-height:1.05}}

    /* Classic horizontal family tree.
       No wrapping: every sibling stays on the same level. */
    .tree, .tree ul, .tree-root{{
      padding-top:24px;
      position:relative;
      display:flex;
      white-space:nowrap;
      margin:0;
    }}

    /* LEFT-EDGE FIX:
       Never center the complete oversized tree inside a smaller box.
       The outer tree begins at the real left edge and expands only rightward. */
    .tree{{
      width:max-content;
      min-width:100%;
      justify-content:flex-start;
      padding-left:0;
      padding-right:0;
    }}

    .tree > .tree-root{{
      width:max-content;
      min-width:max-content;
      justify-content:flex-start;
      padding-left:0;
      padding-right:0;
    }}

    /* Only child groups center below their own parent. */
    .tree-root ul{{
      width:max-content;
      min-width:max-content;
      justify-content:center;
    }}

    .tree-root, .tree-root ul{{
      list-style:none;
      padding-left:0;
    }}

    .tree-root li{{
      list-style:none;
      text-align:center;
      position:relative;
      padding:24px 7px 0;
      flex:0 0 auto;
      white-space:normal;
    }}

    .tree-root li::before,
    .tree-root li::after{{
      content:'';
      position:absolute;
      top:0;
      right:50%;
      width:50%;
      height:24px;
      border-top:2px solid #1765A6;
    }}

    .tree-root li::after{{
      right:auto;
      left:50%;
      border-left:2px solid #1765A6;
    }}

    .tree-root li:only-child::before,
    .tree-root li:only-child::after{{
      display:none;
    }}

    .tree-root li:only-child{{
      padding-top:0;
    }}

    .tree-root li:first-child::before,
    .tree-root li:last-child::after{{
      border:0 none;
    }}

    .tree-root li:last-child::before{{
      border-right:2px solid #1765A6;
      border-radius:0 7px 0 0;
    }}

    .tree-root li:first-child::after{{
      border-radius:7px 0 0 0;
    }}

    .tree-root ul::before{{
      content:'';
      position:absolute;
      top:0;
      left:50%;
      border-left:2px solid #D33D3D;
      width:0;
      height:24px;
    }}

    /* Top A1 connection from Total Cases */
    .root-connector{{
      width:2px;
      height:22px;
      background:#1765A6;
      margin:0 auto -2px;
    }}

    .t7-card-link{{
      display:block;
      text-decoration:none !important;
      color:inherit !important;
      border-radius:14px;
      outline:none;
    }}
    .t7-card-link:hover .bank-card{{
      transform:translateY(-2px);
      box-shadow:0 8px 20px rgba(15,23,42,.12);
      border-color:#2563EB !important;
    }}
    .t7-card-link:focus, .t7-card-link:active{{
      outline:none !important;
      box-shadow:none !important;
    }}
    .bank-card{{
      width:320px;
      min-width:320px;
      background:#fff;
      border:1.5px solid #65A9ED;
      border-radius:10px;
      padding:8px;
      box-shadow:0 3px 10px rgba(31,70,103,.07);
      text-align:left;
      display:inline-block;
      vertical-align:top;
    }}

    .bank-card.a2{{border-color:#8C82EA;background:#FCFBFF}}
    .bank-card.a3{{border-color:#5CBF9C;background:#FBFFFD}}
    .bank-card.a4{{border-color:#66A9E5;background:#FBFDFF}}
    .bank-card.a5{{border-color:#DDA15A;background:#FFFDFC}}

    .bank-head{{
      display:flex;
      align-items:center;
      gap:8px;
      background:linear-gradient(135deg,#EAF3FF,#DDEEFF);
      border:1px solid #83B8EC;
      border-radius:7px;
      padding:10px;
      margin-bottom:8px;
      min-height:62px;
    }}
    .a2 .bank-head{{background:#F0EDFF;border-color:#A69CF1}}
    .a3 .bank-head{{background:#EAF9F3;border-color:#80D0B4}}
    .a4 .bank-head{{background:#EDF6FE;border-color:#8FC0EB}}
    .a5 .bank-head{{background:#FFF5E9;border-color:#E7B779}}

    .attempt-badge{{
      width:40px;height:36px;border-radius:8px;
      background:#1769A5;color:#fff;
      display:flex;align-items:center;justify-content:center;
      font-size:12px;font-weight:900;flex:0 0 auto;
      letter-spacing:.2px;
      text-rendering:geometricPrecision;
      -webkit-font-smoothing:antialiased;
    }}
    .a2 .attempt-badge{{background:#6657CF}}
    .a3 .attempt-badge{{background:#258E6C}}
    .a4 .attempt-badge{{background:#317EBE}}
    .a5 .attempt-badge{{background:#C47B22}}

    .bank-name b{{
      display:block;
      font-size:16px;
      font-weight:900;
      line-height:1.12;
      letter-spacing:.15px;
      color:#082F55;
      overflow-wrap:anywhere;
      text-rendering:geometricPrecision;
      -webkit-font-smoothing:antialiased;
    }}
    .bank-name small{{
      display:block;
      font-size:11px;
      line-height:1.25;
      font-weight:800;
      color:#274E6B;
      margin-top:4px;
      letter-spacing:.05px;
      text-rendering:geometricPrecision;
      -webkit-font-smoothing:antialiased;
    }}

    .routing-summary{{
      margin-left:auto;
      display:flex;
      align-items:center;
      gap:7px;
      flex:0 0 auto;
      background:rgba(255,255,255,.82);
      border:1px solid #BFD5E7;
      border-radius:8px;
      padding:6px 8px;
      min-width:112px;
      justify-content:center;
      box-shadow:0 1px 3px rgba(25,64,96,.04);
    }}
    .routing-item{{
      min-width:37px;
      text-align:center;
      line-height:1;
    }}
    .routing-item span{{
      display:block;
      font-size:7px;
      line-height:1.1;
      font-weight:900;
      letter-spacing:.18px;
      margin-bottom:4px;
    }}
    .routing-item b{{
      display:block;
      font-size:15px;
      line-height:1;
      font-weight:950;
    }}
    .routing-item.pushed span{{color:#17683D}}
    .routing-item.pushed b{{color:#08783D}}
    .routing-item.left span{{color:#A33B3B}}
    .routing-item.left b{{color:#C02E2E}}
    .routing-divider{{
      width:1px;
      height:26px;
      background:#D7E2EA;
    }}

    .route-line{{
      background:#F8F3FF;
      border:1px solid #DED5F8;
      color:#563D7C;
      border-radius:5px;
      padding:6px 7px;
      margin-bottom:7px;
      font-size:9px;
      line-height:1.25;
      font-weight:800;
      overflow-wrap:anywhere;
    }}
    .route-line span{{font-size:8px;font-weight:900;color:#983747}}
    .route-line b{{color:#D13D3D;padding:0 3px}}

    .status-grid{{
      display:grid;
      grid-template-columns:repeat(3,1fr);
      gap:6px;
    }}
    .status{{
      border:1px solid;
      border-radius:7px;
      text-align:center;
      padding:7px 2px;
    }}
    .status span{{display:block;font-size:10px;font-weight:900;letter-spacing:.05px}}
    .status b{{display:block;font-size:21px;font-weight:900;line-height:1.05;margin:3px 0}}
    .status small{{font-size:10px;font-weight:850}}
    .app{{background:#E5F8EC;border-color:#77D09A;color:#17683D}}
    .pend{{background:#FFF4DB;border-color:#E8BA50;color:#855500}}
    .rej{{background:#FDE8E8;border-color:#EF7D7D;color:#A72D2D}}

    .detail-grid{{
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:6px;
      margin-top:7px;
    }}
    .detail{{
      min-width:0;
      border-radius:7px;
      padding:7px;
      min-height:94px;
    }}
    .pending-detail{{
      background:#FFF9E9;
      border:1px solid #EAC86D;
      color:#6E4A0B;
    }}
    .reject-detail{{
      background:#FFF0F0;
      border:1px solid #EF9999;
      color:#8D3030;
    }}
    .detail-title{{
      font-size:9px;
      line-height:1.2;
      font-weight:900;
      text-transform:uppercase;
      margin-bottom:6px;
      letter-spacing:.12px;
    }}
    .mini-row{{
      display:grid;
      grid-template-columns:minmax(0,1fr) auto;
      gap:7px;
      font-size:9px;
      font-weight:750;
      line-height:1.35;
      margin:4px 0;
    }}
    .mini-row span{{white-space:normal;overflow-wrap:anywhere}}
    .mini-row b{{font-weight:950}}
    .mini-row.other{{
      border-top:1px dashed rgba(130,60,60,.25);
      padding-top:3px;
    }}
    .empty{{font-size:7px;font-weight:800;opacity:.7}}

    .terminal{{
      width:170px;
      margin:7px auto 0;
      padding:5px 7px;
      border:1px dashed #D3DEE7;
      border-radius:6px;
      background:#FAFCFD;
      color:#718697;
      font-size:7px;
      font-weight:800;
      text-align:center;
    }}

    .attempt-legend{{
      position:sticky;
      left:14px;
      z-index:40;
      display:flex;
      gap:5px;
      width:max-content;
      margin-top:10px;
    }}
    .attempt-legend span{{
      border-radius:5px;
      padding:4px 7px;
      font-size:7px;
      font-weight:950;
      color:#fff;
    }}
    .lg1{{background:#1769A5}} .lg2{{background:#6657CF}}
    .lg3{{background:#258E6C}} .lg4{{background:#317EBE}} .lg5{{background:#C47B22}}

    /* Make horizontal scrollbar obvious and usable. */
    .viewport::-webkit-scrollbar{{height:14px}}
    .viewport::-webkit-scrollbar-track{{background:#E6EEF4;border-radius:10px}}
    .viewport::-webkit-scrollbar-thumb{{background:#6F9FBE;border-radius:10px;border:3px solid #E6EEF4}}
    </style>
    </head>
    <body>
      <div class="viewport">
        <div class="canvas">
          <div class="scroll-note">↔ Scroll horizontally — all lenders and all A1→A5 branches are preserved</div>
          <div class="root-wrap">
            <div class="root"><small>TOTAL CASES</small><b>{total_cases:,}</b></div>
          </div>
          <div class="root-connector"></div>

          <div class="tree">
            {tree_html}
          </div>

          <div class="attempt-legend">
            <span class="lg1">A1 First Lender</span>
            <span class="lg2">A2 Second Lender</span>
            <span class="lg3">A3 Third Lender</span>
            <span class="lg4">A4 Fourth Lender</span>
            <span class="lg5">A5 Fifth Lender</span>
          </div>
        </div>
      </div>
    </body>
    </html>
    """

    # STREAM 6.1 — EXACT 5.1 TABLE 7 + FRAGMENT-LOCAL CLICK FLOW
    # The visual board stays unchanged. The click receiver lives inside an
    # st.fragment so clicking a lender card reruns only this tiny interaction
    # block instead of rebuilding the complete dashboard. The dialog itself is
    # also fragment-scoped by Streamlit, so its native X closes immediately.
    _t7_click_js = r"""
    export default function(component) {
      const { parentElement, setTriggerValue } = component;
      const links = parentElement.querySelectorAll('[data-t7node]');
      const handlers = [];

      links.forEach((link) => {
        const handler = (event) => {
          event.preventDefault();
          event.stopPropagation();
          const nodeId = link.getAttribute('data-t7node');
          if (nodeId) setTriggerValue('clicked', nodeId);
        };
        link.addEventListener('click', handler, { passive: false });
        handlers.push([link, handler]);
      });

      return () => {
        handlers.forEach(([link, handler]) => link.removeEventListener('click', handler));
      };
    }
    """

    @st.fragment
    def _render_table7_click_surface():
        try:
            _t7_component = st.components.v2.component(
                "rengy_table7_credit_journey_v61",
                html=board_html,
                js=_t7_click_js,
                isolate_styles=True,
            )
            _t7_result = _t7_component(
                key="rengy_table7_credit_journey_v61_instance",
                on_clicked_change=lambda: None,
            )
            _clicked = clean_text(getattr(_t7_result, "clicked", ""))
        except AttributeError:
            st.error(
                "Table 7 direct popup requires Streamlit 1.51+ (Components V2). "
                "Update Streamlit in requirements.txt, then redeploy."
            )
            return

        if _clicked in _t7_node_id_to_route:
            _route = _t7_node_id_to_route[_clicked]
            _node = route_nodes.get(_route)
            if _node is not None:
                _show_t7_node_dialog(_route, _node)

    _render_table7_click_surface()

    st.caption(
        "Full horizontal family tree: no lender is hidden, wrapped into another band, or reduced to a tiny chip. "
        "Every A1–A5 node uses the same readable card and rejected cases continue through their actual next lender."
    )


# ============================================================
# 47. INTERACTIVE CASE DETAILS — EXCEL-STYLE HEADER FILTERS
# ============================================================

st.markdown("### Interactive Case Details")

# SAME BUSINESS LOGIC AS BEFORE:
# interactive details contain cases that are either a login in the
# selected reporting period OR a confirmed disbursement in the period,
# after all top-level dashboard filters have already been applied.
detail_scope = filtered.loc[
    login_in_period
    | disbursed_in_period
].copy()

# ------------------------------------------------------------
# KEEP ALL DATA / ALL AVAILABLE COLUMNS
# ------------------------------------------------------------
# Put the useful business columns first, then append every remaining
# master-data column. No case/column is intentionally limited here.
detail_priority_columns = [
    "LEAD ID",
    "Lead Name",
    "Mobile",
    "Mobile Number",
    "mobileNumber",
    "Vendor/Channel Partner Name",
    "Consultant Name",
    "Region",
    "Lead District",
    "Requested Date",
    "Project Value",
    "Bank/NBFC",
    "Credit Officer",
    "Fintech ID",
    "Bank Name",
    "IFSC Code",
    "Branch",
    "Approved Amount",
    "Disbursed Amount",
    "Loan Pending Amount",
    "Login Done On",
    "Disbursed At",
    "Current Status",
    "Loan Sub Stage",
    "Login Attempts",
    "Comments",
    "Dynamic Pricing",
    "Source",
]

for suffix in ATTEMPT_SUFFIXES:
    detail_priority_columns.extend(
        [
            f"{suffix} Date",
            f"{suffix} Provider",
            f"{suffix} Stage",
            f"{suffix} Substage",
            f"{suffix} Remark",
        ]
    )

detail_columns = []

for col in detail_priority_columns:
    if (
        col in detail_scope.columns
        and col not in detail_columns
    ):
        detail_columns.append(col)

for col in detail_scope.columns:
    if (
        col not in detail_columns
        and not str(col).startswith("_")
    ):
        detail_columns.append(col)

detail_view = detail_scope[
    detail_columns
].copy()

# Clean complex/object values so the browser grid can display/filter them.
for col in detail_view.columns:
    if pd.api.types.is_datetime64_any_dtype(
        detail_view[col]
    ):
        detail_view[col] = (
            detail_view[col]
            .dt.strftime(
                "%d-%m-%Y %H:%M"
            )
            .fillna("")
        )

    elif detail_view[col].dtype == "object":
        detail_view[col] = detail_view[col].apply(
            lambda value:
                ""
                if value is None
                else (
                    str(value)
                    if isinstance(
                        value,
                        (
                            dict,
                            list,
                            tuple,
                            set,
                        ),
                    )
                    else value
                )
        )

# ------------------------------------------------------------
# DISPLAY PAGE SIZE ONLY — DOES NOT LIMIT ANALYSIS DATA
# ------------------------------------------------------------
detail_rows_per_page = st.selectbox(
    "Rows per page",
    [25, 50, 75, 100],
    index=3,
    key="interactive_excel_rows_per_page",
)

# ------------------------------------------------------------
# EXCEL-LIKE GRID
# ------------------------------------------------------------
detail_gb = GridOptionsBuilder.from_dataframe(
    detail_view
)

detail_gb.configure_default_column(
    sortable=True,
    filter=True,
    resizable=True,
    floatingFilter=False,
    minWidth=125,
)

for col in detail_view.columns:
    if pd.api.types.is_numeric_dtype(
        detail_view[col]
    ):
        detail_gb.configure_column(
            col,
            filter="agNumberColumnFilter",
            filterParams={
                "buttons": [
                    "apply",
                    "clear",
                ],
                "closeOnApply": True,
            },
        )
    else:
        # Excel-style searchable checklist:
        # Search / Select All / individual values / Apply / Clear.
        detail_gb.configure_column(
            col,
            filter="agSetColumnFilter",
            filterParams={
                "buttons": [
                    "apply",
                    "clear",
                ],
                "closeOnApply": True,
                "excelMode": "windows",
                "suppressSelectAll": False,
                "suppressMiniFilter": False,
            },
        )

detail_gb.configure_pagination(
    paginationAutoPageSize=False,
    paginationPageSize=int(
        detail_rows_per_page
    ),
)

detail_gb.configure_grid_options(
    suppressRowClickSelection=True,
    animateRows=False,
    enableCellTextSelection=True,
    ensureDomOrder=True,
    sideBar={
        "toolPanels": [
            {
                "id": "filters",
                "labelDefault": "Filters",
                "labelKey": "filters",
                "iconKey": "filter",
                "toolPanel": "agFiltersToolPanel",
            },
            {
                "id": "columns",
                "labelDefault": "Columns",
                "labelKey": "columns",
                "iconKey": "columns",
                "toolPanel": "agColumnsToolPanel",
            },
        ],
        "defaultToolPanel": "",
    },
)

detail_grid_options = detail_gb.build()

detail_grid_response = AgGrid(
    detail_view,
    gridOptions=detail_grid_options,
    update_mode=GridUpdateMode.FILTERING_CHANGED,
    data_return_mode=DataReturnMode.FILTERED,
    fit_columns_on_grid_load=False,
    allow_unsafe_jscode=False,
    enable_enterprise_modules=True,
    theme="streamlit",
    height=620,
    key="credit_interactive_excel_grid",
)

detail_filtered_data = detail_grid_response.get(
    "data",
    detail_view,
)

if detail_filtered_data is None:
    detail_filtered_data = detail_view.copy()

if not isinstance(
    detail_filtered_data,
    pd.DataFrame,
):
    detail_filtered_data = pd.DataFrame(
        detail_filtered_data
    )

detail_filtered_count = int(
    len(detail_filtered_data)
)

st.info(
    f"Showing **{detail_filtered_count:,} records** "
    f"(out of **{len(detail_view):,}**) after applying column filters."
)

detail_download_left, detail_download_right = st.columns(
    [1.45, 4.55]
)

with detail_download_left:
    st.download_button(
        "⬇ Download Filtered Data",
        data=detail_filtered_data.to_csv(
            index=False
        ).encode(
            "utf-8-sig"
        ),
        file_name=(
            "rengy_credit_interactive_"
            "case_details_filtered.csv"
        ),
        mime="text/csv",
        use_container_width=True,
        key="credit_interactive_excel_download",
    )

# ============================================================
# STREAM 4.0 — COMPACT ROW 2 + SECTION GAP CONTROLS
# ============================================================
st.markdown(r"""
<style>
/* Stable vertical rhythm: title -> caption -> content. */
.main h3 {
    margin: 20px 0 8px 0 !important;
    padding: 0 !important;
    line-height: 1.22 !important;
    min-height: 0 !important;
    position: relative !important;
    z-index: 2 !important;
}
.main [data-testid="stCaptionContainer"] {
    margin: 0 0 12px 0 !important;
    padding: 0 !important;
    line-height: 1.35 !important;
    min-height: 18px !important;
    position: relative !important;
    z-index: 2 !important;
}
/* Never pull charts/tables upward into titles. */
.main [data-testid="stPlotlyChart"],
.main [data-testid="stDataFrame"],
.main iframe {
    margin-top: 6px !important;
    position: relative !important;
    z-index: 1 !important;
}
/* Row 2: clear, consistent title lane in both columns. */
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) h3,
#credit-row2-anchor ~ div h3 {
    margin-top: 8px !important;
    margin-bottom: 4px !important;
    padding: 0 !important;
    line-height: 1.18 !important;
    min-height: 27px !important;
}
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) [data-testid="stCaptionContainer"],
#credit-row2-anchor ~ div [data-testid="stCaptionContainer"] {
    display:block !important;
    height:auto !important;
    margin:0 0 6px 0 !important;
    padding:0 !important;
    line-height:1.28 !important;
}
div[data-testid="stVerticalBlock"]:has(#credit-row2-anchor) [data-testid="stPlotlyChart"],
#credit-row2-anchor ~ div [data-testid="stPlotlyChart"] {
    margin-top: 2px !important;
    margin-bottom: 6px !important;
}
/* Table 5: force the complete heading/filter row to plain white. */
div[data-testid="stHorizontalBlock"]:has(#table5-clean-header),
div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) > div,
div[data-testid="stVerticalBlock"]:has(#table5-clean-header),
div[data-testid="stElementContainer"]:has(#table5-clean-header) {
    background:#FFFFFF !important;
    background-color:#FFFFFF !important;
    background-image:none !important;
    border:0 !important;
    box-shadow:none !important;
}
div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) {
    padding: 4px 0 6px 0 !important;
    margin: 2px 0 4px 0 !important;
}
div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) h3 {
    margin: 8px 0 4px 0 !important;
    color:#111827 !important;
}
/* Table 7 gets an unmistakable break from the component above. */
div[data-testid="stElementContainer"]:has(#table7-spacing-anchor) {
    margin-top: 30px !important;
    margin-bottom: 8px !important;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# STREAM 4.2 — FINAL ROW-2 RHYTHM / COMPACT SECTION SPACING
# ============================================================
st.markdown(r"""
<style>
/* Row 1 intentionally untouched. Explicit 24px spacers above control Row-2 title-to-content gap. */
/* Pull the lower analytics blocks together without overlap. */
div[data-testid="stElementContainer"]:has(#table5-clean-header) {
    margin-top: 6px !important;
}
div[data-testid="stHorizontalBlock"]:has(#table5-clean-header) {
    margin-top: 0 !important;
    margin-bottom: 3px !important;
    padding-top: 0 !important;
    padding-bottom: 3px !important;
}
/* Keep a small deliberate break before Table 7, not a large blank band. */
div[data-testid="stElementContainer"]:has(#table7-spacing-anchor) {
    margin-top: 16px !important;
    margin-bottom: 4px !important;
}
/* Compact Plotly/dataframe bottoms in Row 2. */
div[data-testid="stHorizontalBlock"] [data-testid="stPlotlyChart"],
div[data-testid="stHorizontalBlock"] [data-testid="stDataFrame"] {
    margin-bottom: 3px !important;
}
</style>
""", unsafe_allow_html=True)
