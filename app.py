# app.py
import io
import os
import time
from datetime import datetime

import streamlit as st
import streamlit_authenticator as stauth

from streamlit_folium import st_folium
import folium

import pandas as pd
import requests
import streamlit as st

def require_login():
    cfg = st.secrets["auth"]
    users = cfg["users"]  # list of {name, username, password}

    # Build credentials exactly as streamlit-authenticator expects.
    # Normalize usernames: lowercase + strip whitespace.
    credentials = {
        "usernames": {
            (u["username"] or "").strip().lower(): {
                "name": u["name"],
                "password": u["password"],  # bcrypt hash
            }
            for u in users
            if u.get("username") and u.get("password")
        }
    }

    authenticator = stauth.Authenticate(
        credentials,
        cfg["cookie_name"],
        cfg["cookie_key"],
        cfg.get("cookie_expiry_days", 7),
    )

    # Newer API: pass location and (optionally) labels via fields
    fields = {
        "Form name": "Login",
        "Username": "Username",
        "Password": "Password",
        "Login": "Sign in",
    }
    name, auth_status, username = authenticator.login(location="main", fields=fields)

    if auth_status is False:
        st.error("Username/password is incorrect")
        st.stop()
    if auth_status is None:
        st.info("Please log in to continue.")
        st.stop()

    with st.sidebar:
        st.caption(f"Signed in as **{name}**")
        authenticator.logout("Log out", "sidebar")


require_login()


# ===============================
# API endpoints (Places API — New)
# ===============================
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL_TPL = "https://places.googleapis.com/v1/places/{place_id}"


# ===============================
# Key loading (env -> secrets -> UI)
# ===============================
def get_maps_api_key():
    # 1) Environment variable
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if key:
        return key, "(loaded from environment)"
    # 2) Streamlit secrets (guarded)
    try:
        if "GOOGLE_MAPS_API_KEY" in st.secrets:
            return st.secrets["GOOGLE_MAPS_API_KEY"], "(loaded from st.secrets)"
    except Exception:
        pass
    # 3) Manual entry
    return "", None


# ===============================
# Places API (New) helpers
# ===============================
def fetch_businesses_v1(
    api_key: str,
    query: str,
    lat: float,
    lng: float,
    radius_m: int,
    max_pages: int = 3,
):
    """
    Text Search (New). We request websiteUri in the field mask so we can
    filter out businesses that already have a website BEFORE hitting Details.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ",".join(
            [
                "places.id",
                "places.displayName",
                "places.formattedAddress",
                "places.rating",
                "places.userRatingCount",
                "places.businessStatus",
                "places.websiteUri",      # <— crucial for pre-filtering
                "nextPageToken",
            ]
        ),
    }

    payload = {
        "textQuery": query,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_m),
            }
        },
        "pageSize": 20,
    }

    results, pages_fetched = [], 0
    page_token = None

    while True:
        body = dict(payload)
        if page_token:
            body["pageToken"] = page_token

        resp = requests.post(PLACES_TEXT_URL, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            st.error(f"Text Search HTTP error: {resp.status_code}\n{resp.text}")
            break

        data = resp.json()
        for p in data.get("places", []):
            results.append(
                {
                    "name": (p.get("displayName") or {}).get("text", "N/A"),
                    "address": p.get("formattedAddress", "N/A"),
                    "rating": p.get("rating", "N/A"),
                    "user_ratings_total": p.get("userRatingCount", 0),
                    "business_status": p.get("businessStatus", "N/A"),
                    "place_id": p.get("id"),
                    "website": p.get("websiteUri") or "",  # <— capture website here
                }
            )

        page_token = data.get("nextPageToken")
        pages_fetched += 1
        if not page_token or pages_fetched >= max_pages:
            break

        time.sleep(1.0)  # small courtesy pause

    return results

# ===============================
# Map Implimentation
# ===============================
def pick_location_on_map(initial_lat: float, initial_lng: float, radius_m: int):
    """
    Renders a Leaflet map; user clicks to set center point.
    Returns (lat, lng). Draws a circle for the radius.
    """
    lat = float(initial_lat)
    lng = float(initial_lng)

    # Build map centered on current lat/lng
    m = folium.Map(location=[lat, lng], zoom_start=14, control_scale=True, tiles="OpenStreetMap")
    # Current center marker
    folium.Marker([lat, lng], tooltip="Search center").add_to(m)
    # Radius overlay
    folium.Circle([lat, lng], radius=radius_m, color="#3388ff", fill=True, fill_opacity=0.15).add_to(m)

    # Render and capture clicks
    out = st_folium(m, height=420, width=None, returned_objects=["last_clicked"])

    # If user clicked, update center
    click = (out or {}).get("last_clicked")
    if click and "lat" in click and "lng" in click:
        lat = float(click["lat"])
        lng = float(click["lng"])
    return lat, lng

def get_detailed_info_v1(api_key: str, place_id: str, retries: int = 3):
    """
    Place Details (New). Ask only for fields we display.
    """
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ",".join(
            [
                "id",
                "displayName",
                "formattedAddress",
                "internationalPhoneNumber",
                "nationalPhoneNumber",
                "websiteUri",
                "googleMapsUri",
                "rating",
                "userRatingCount",
                "businessStatus",
            ]
        ),
    }
    url = PLACES_DETAILS_URL_TPL.format(place_id=place_id)

    for _ in range(retries):
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json()
        time.sleep(1.2)

    return {}


def enrich_with_details_v1(api_key: str, raw_places: list):
    """
    For the (smaller) subset with no website from search results, fetch Details
    to get phone numbers and finalize rows.
    """
    enriched = []
    if not raw_places:
        return enriched

    progress = st.progress(0.0, text="Fetching place details…")
    for i, b in enumerate(raw_places, start=1):
        d = get_detailed_info_v1(api_key, b["place_id"])

        national = d.get("nationalPhoneNumber")
        international = d.get("internationalPhoneNumber")
        phone = national or international or "N/A"

        # Re-check website in Details just in case it appears here:
        website = d.get("websiteUri") or b.get("website") or ""  # keep empty for "no site"

        enriched.append(
            {
                "Name": (d.get("displayName") or {}).get("text") or b["name"],
                "Address": d.get("formattedAddress", b["address"]),
                "Rating": d.get("rating", b.get("rating")),
                "User Ratings": d.get("userRatingCount", b.get("user_ratings_total")),
                "Business Status": d.get("businessStatus", b.get("business_status")),
                "Phone (national)": national or "",
                "Phone (intl)": international or "",
                "Phone": phone,
                "Website": website,
                "Google Maps URL": d.get("googleMapsUri", "N/A"),
                "Place ID": d.get("id", b["place_id"]),
                "Fetched At": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
        )

        progress.progress(
            i / len(raw_places),
            text=f"Fetching place details… ({i}/{len(raw_places)})",
        )

    progress.empty()
    return enriched


# ===============================
# File export helper
# ===============================
def dataframe_to_excel_bytes(df: pd.DataFrame):
    try:
        import openpyxl  # noqa: F401
        from pandas import ExcelWriter

        buf = io.BytesIO()
        with ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="Results")
        buf.seek(0)
        return {
            "bytes": buf.getvalue(),
            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "filename": "business_list.xlsx",
        }
    except Exception:
        try:
            import xlsxwriter  # noqa: F401
            from pandas import ExcelWriter

            buf = io.BytesIO()
            with ExcelWriter(buf, engine="xlsxwriter") as w:
                df.to_excel(w, index=False, sheet_name="Results")
            buf.seek(0)
            return {
                "bytes": buf.getvalue(),
                "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "filename": "business_list.xlsx",
            }
        except Exception:
            csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
            return {
                "bytes": csv_bytes,
                "mime": "text/csv",
                "filename": "business_list.csv",
            }


# ===============================
# Streamlit UI
# ===============================
st.set_page_config(
    page_title="Google Places — No-Website Finder", 
    page_icon="🔎", 
    layout="wide")

st.title("🔎 Google Places — Companies *without* websites (Places API New)")
st.markdown(
    "This app uses **Text Search (New)** to detect websites, filters them out, "
    "and only calls **Place Details (New)** for the remainder to fetch phone numbers."
)

api_key, source = get_maps_api_key()

with st.sidebar:
    st.header("Settings")

    if api_key:
        st.text_input("Google Maps API Key", value=source, type="password", disabled=True)
    else:
        api_key = st.text_input("Google Maps API Key", type="password",
                                help="Set env var or Streamlit secrets to avoid typing this.")

    query = st.text_input("Keyword / Query", value="Software Solutions")
    radius = st.slider("Radius (meters)", 100, 50000, 1000, 100)
    max_pages = st.select_slider("Pages to fetch (× ~20 results)", [1, 2, 3], value=3)
    only_no_website = st.checkbox("Only include places with no website", value=True)
    

    # ---- Map (click to drop pin) ----
    default_lat, default_lng = -25.786290, 28.283747
    lat0 = st.session_state.get("last_lat", default_lat)
    lng0 = st.session_state.get("last_lng", default_lng)

    st.caption("Click on the map to drop a pin (center of search).")
    lat, lng = pick_location_on_map(initial_lat=lat0, initial_lng=lng0, radius_m=radius)

    # remember for next rerun
    st.session_state["last_lat"] = lat
    st.session_state["last_lng"] = lng

    st.write(f"**Center:** {lat:.6f}, {lng:.6f}")

    run_btn = st.button("Search & Build File", type="primary")

if run_btn:
    if not api_key:
        st.error("Please add your Google Maps API key.")
        st.stop()

    with st.spinner("Searching places (Text Search, v1)…"):
        places = fetch_businesses_v1(api_key, query, lat, lng, radius, max_pages=max_pages)
        st.success(f"Search results: {len(places)} places total.")

    # Filter BEFORE Details to reduce requests
    def has_site(x: object) -> bool:
        """
        True if a real website exists. Treat '', None, 'N/A', 'NA', 'NONE', 'NULL'
        (any casing/whitespace) as *no site*.
        """
        if x is None:
            return False
        s = str(x).strip()
        if not s:
            return False
        return s.upper() not in {"N/A", "NA", "NONE", "NULL"}

    # place this helper near the top of the file, then use it here
    if only_no_website:
        places_no_site = [p for p in places if not has_site(p.get("website"))]
        st.info(f"Filtering out places with websites → {len(places_no_site)} left for enrichment.")
    else:
        places_no_site = places


    if not places_no_site:
        st.warning("No places without websites were found for the given search.")
        st.stop()

    with st.spinner("Fetching phone numbers (Place Details, v1)…"):
        enriched = enrich_with_details_v1(api_key, places_no_site)

    # Safety: drop any that turn out to *have* a website after Details
    if only_no_website:
        enriched = [row for row in enriched if not has_site(row.get("Website"))]

    df = pd.DataFrame(enriched)

    st.subheader("Preview")
    st.dataframe(df, use_container_width=True)

    out = dataframe_to_excel_bytes(df)
    st.download_button(
        "📥 Download results",
        data=out["bytes"],
        file_name=out["filename"],
        mime=out["mime"],
        use_container_width=True,
    )

    st.caption(
        "If no Excel engine is installed, a CSV is provided. Install openpyxl or xlsxwriter for XLSX."
    )

with st.expander("Notes"):
    st.markdown(
        """
- **Request minimization:** We request `websiteUri` in **Text Search** so we can skip **Place Details** for businesses that already have a site.
- **Phones:** Google doesn’t label numbers as mobile vs landline; we show national & international formats when available.
- **Cost control:** Field masks keep responses small; we only ask for the fields we use.
"""
    )
