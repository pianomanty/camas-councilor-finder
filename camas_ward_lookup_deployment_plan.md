# Camas City Councilor Lookup — Raspberry Pi Web App Plan

## Overview
Deploy the Python/GeoPandas Camas ward lookup tool as a lightweight Flask JSON API running alongside WordPress on a Raspberry Pi 4 (8 GB RAM). WordPress serves the user-facing HTML/JavaScript form and calls the local Flask backend via reverse proxy on the same domain (avoiding CORS issues).

---

## 1. Project Directory Structure
Create a dedicated folder on the Raspberry Pi (e.g., `/home/pi/camas-ward-api/`):

```text
camas-ward-api/
├── app.py
├── ward_lookup.py
└── Data/
    ├── councilors.json
    ├── ward_boundaries.geojson
    └── geocode_cache.json   (created automatically on first lookup)
```

---

## 2. Data Configuration (`Data/councilors.json`)
Store ward-to-councilor mappings separately from application logic so updates after elections require no code changes:

```json
{
  "wards": {
    "1": ["John Nohr", "Marilyn Boerke"],
    "2": ["Martin Elzingre", "Tim Hein"],
    "3": ["Jennifer Senescu", "Mahsa Eshghi"]
  },
  "at_large": "John Svilarich"
}
```

---

## 3. Core GIS & Geocoding Module (`ward_lookup.py`)
Key features built into `ward_lookup.py`:
- **Address Standardization (`usaddress`):** Strips apartment/unit numbers for cleaner street-level geocoding and defaults missing city/state fields to `Camas, WA` without overriding explicitly entered cities or forcing a single ZIP code.
- **Viewbox Bias & Rate Limiting:** Uses a Camas bounding box (`viewbox` with `bounded=False`) and enforces Nominatim's 1-request-per-second limit.
- **Local Caching (`Data/geocode_cache.json`):** Stores previously geocoded standardized addresses to minimize external API calls.
- **Boundary Overlap & Out-of-Limits Handling:** Detects points falling directly on a shared ward boundary and provides a clear message when an address is outside Camas city limits (e.g., unincorporated Clark County).

```python
from functools import partial
import json
from pathlib import Path
from typing import Any

from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
import geopandas as gpd
import pandas as pd
from probableparsing import RepeatedLabelError
import usaddress

DATA_DIR = Path(__file__).resolve().parent / "Data"
DEFAULT_WARD_GEOJSON = DATA_DIR / "ward_boundaries.geojson"
DEFAULT_COUNCILORS_JSON = DATA_DIR / "councilors.json"
DEFAULT_CACHE_JSON = DATA_DIR / "geocode_cache.json"

# Bounding box around Camas, WA as (latitude, longitude) corners for GeoPy
CAMAS_VIEWBOX = [(45.56, -122.48), (45.66, -122.34)]


def clean_us_address(
    raw_address: str | None,
    default_city: str = "Camas",
    default_state: str = "WA",
) -> str | None:
    """Parse a US address, strip unit numbers, and default missing city/state to Camas, WA."""
    if not isinstance(raw_address, str) or not raw_address.strip():
        return None

    try:
        tagged_dict, _ = usaddress.tag(raw_address)
    except RepeatedLabelError:
        text = raw_address.strip()
        return text if "," in text else f"{text}, {default_city}, {default_state}"

    street_parts = [
        tagged_dict.get("AddressNumber", ""),
        tagged_dict.get("StreetNamePreDirectional", ""),
        tagged_dict.get("StreetName", ""),
        tagged_dict.get("StreetNamePostType", ""),
        tagged_dict.get("StreetNamePostDirectional", ""),
    ]
    street_line = " ".join(part for part in street_parts if part).strip()

    city = tagged_dict.get("PlaceName") or default_city
    state = tagged_dict.get("StateName") or default_state
    zip_code = tagged_dict.get("ZipCode", "")
    city_state_zip = f"{city}, {state} {zip_code}".strip()

    if street_line:
        return f"{street_line}, {city_state_zip}"
    return raw_address.strip()


def build_geocoder(
    user_agent: str = "camas_ward_lookup_web/1.0 (contact@yourdomain.com)",
    viewbox: list[tuple[float, float]] = CAMAS_VIEWBOX,
):
    """Initialize a rate-limited Nominatim geocoder biased toward Camas, WA."""
    geolocator = Nominatim(user_agent=user_agent)
    biased_geocode = partial(
        geolocator.geocode,
        viewbox=viewbox,
        bounded=False,
        country_codes="us",
    )
    return RateLimiter(biased_geocode, min_delay_seconds=1.0)


def load_geocode_cache(cache_path: Path = DEFAULT_CACHE_JSON) -> dict[str, dict[str, Any]]:
    """Load previously geocoded addresses from a local JSON cache."""
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_geocode_cache(
    cache: dict[str, dict[str, Any]],
    cache_path: Path = DEFAULT_CACHE_JSON,
) -> None:
    """Persist geocoded address results to disk."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def geocode_address(
    raw_address: str,
    geocode_fn,
    cache_path: Path = DEFAULT_CACHE_JSON,
) -> gpd.GeoDataFrame:
    """Clean and geocode a single address (using local cache if available)."""
    cleaned = clean_us_address(raw_address)
    if not cleaned:
        raise ValueError("Please enter a valid non-empty address.")

    cache = load_geocode_cache(cache_path)
    cached_hit = cache.get(cleaned)

    if cached_hit is not None:
        matched_address = cached_hit["matched_address"]
        latitude = cached_hit["latitude"]
        longitude = cached_hit["longitude"]
    else:
        location = geocode_fn(cleaned)
        if location is None:
            raise ValueError(f"Could not geocode address: '{cleaned}'")

        matched_address = location.address
        latitude = location.latitude
        longitude = location.longitude

        cache[cleaned] = {
            "matched_address": matched_address,
            "latitude": latitude,
            "longitude": longitude,
        }
        save_geocode_cache(cache, cache_path)

    df = pd.DataFrame(
        [
            {
                "raw_input": raw_address.strip(),
                "clean_address": cleaned,
                "matched_address": matched_address,
                "latitude": latitude,
                "longitude": longitude,
            }
        ]
    )
    return gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs="EPSG:4326",
    )


def load_councilor_roster(
    councilors_path: Path = DEFAULT_COUNCILORS_JSON,
) -> tuple[dict[int, list[str]], str]:
    """Load ward and at-large councilor mappings from JSON."""
    with councilors_path.open("r", encoding="utf-8") as f:
        roster = json.load(f)

    ward_councilors = {
        int(ward_num): names for ward_num, names in roster["wards"].items()
    }
    at_large_councilor = roster["at_large"]
    return ward_councilors, at_large_councilor


def load_ward_data(
    geojson_path: Path = DEFAULT_WARD_GEOJSON,
    councilors_path: Path = DEFAULT_COUNCILORS_JSON,
) -> gpd.GeoDataFrame:
    """Load Camas ward polygons and attach ward and at-large councilor info."""
    ward_councilors, at_large_councilor = load_councilor_roster(councilors_path)

    ward_data = gpd.read_file(geojson_path)
    ward_data = ward_data.dropna(subset=["Ward", "geometry"]).copy()

    ward_nums = ward_data["Ward"].astype(int)
    councilor_labels = {
        ward: " & ".join(names) for ward, names in ward_councilors.items()
    }

    ward_data["Ward_Cat"] = "Ward " + ward_nums.astype(str)
    ward_data["Councilors"] = ward_nums.map(councilor_labels)
    ward_data["At_Large"] = at_large_councilor

    return ward_data


def find_wards(
    address_gdf: gpd.GeoDataFrame,
    ward_data: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Return matching ward row(s) for a point GeoDataFrame, or raise ValueError."""
    if ward_data.crs is None:
        raise ValueError("Ward boundary data is missing a coordinate reference system (CRS).")

    if address_gdf.crs != ward_data.crs:
        address_gdf = address_gdf.to_crs(ward_data.crs)

    joined = gpd.sjoin(
        address_gdf,
        ward_data[["Ward_Cat", "Councilors", "At_Large", "geometry"]],
        how="inner",
        predicate="intersects",
    ).drop(columns=["index_right"], errors="ignore")

    if joined.empty:
        raise ValueError(
            "This address is outside Camas city limits (for example, in unincorporated "
            "Clark County) and is not represented by a Camas city ward."
        )

    return joined
```

---

## 4. Flask API (`app.py`)
Loads the GeoJSON and councilor roster once on startup and uses a `threading.Lock` around geocoding/cache writes so simultaneous visitors respect Nominatim's rate limit.

```python
from threading import Lock
from flask import Flask, jsonify, request

from ward_lookup import (
    build_geocoder,
    find_wards,
    geocode_address,
    load_ward_data,
)

app = Flask(__name__)

WARD_DATA = load_ward_data()
GEOCODE_FN = build_geocoder(
    user_agent="camas_ward_lookup_web/1.0 (contact@yourdomain.com)"
)
GEOCODE_LOCK = Lock()

MAX_ADDRESS_LENGTH = 200


@app.get("/api/health")
def health_check():
    return jsonify({"status": "ok", "wards_loaded": len(WARD_DATA)})


@app.post("/api/lookup")
def lookup_ward():
    payload = request.get_json(silent=True) or {}
    raw_address = payload.get("address", "")

    if not isinstance(raw_address, str) or not raw_address.strip():
        return jsonify({"error": "Please enter a street address."}), 400

    if len(raw_address) > MAX_ADDRESS_LENGTH:
        return jsonify({"error": "Address is too long."}), 400

    try:
        with GEOCODE_LOCK:
            address_gdf = geocode_address(raw_address, GEOCODE_FN)
        matches = find_wards(address_gdf, WARD_DATA)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception:
        return jsonify({"error": "Unexpected error looking up address."}), 500

    wards = [
        {
            "ward": row["Ward_Cat"],
            "councilors": row["Councilors"],
        }
        for _, row in matches.iterrows()
    ]

    return jsonify(
        {
            "matched_address": matches["matched_address"].iloc[0],
            "on_boundary": len(wards) > 1,
            "wards": wards,
            "at_large": matches["At_Large"].iloc[0],
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
```

---

## 5. Raspberry Pi Setup & Testing

### Install Dependencies in a Virtual Environment
```bash
cd ~/camas-ward-api
python3 -m venv venv
source venv/bin/activate
pip install flask gunicorn geopandas geopy pandas usaddress
```
*(Alternative if building GeoPandas wheels is slow on Pi OS: `sudo apt install python3-geopandas` and create the venv with `python3 -m venv --system-site-packages venv`.)*

### Local Smoke Test
Run `python app.py` and test in another terminal:
```bash
curl -X POST http://127.0.0.1:8000/api/lookup \
  -H "Content-Type: application/json" \
  -d '{"address": "616 NE 4th Ave"}'
```

---

## 6. Reverse Proxy Configuration (Nginx or Apache)
Check which web server is running on ports 80/443:
```bash
sudo ss -tulpn | grep -E ':80|:443'
```

### If Nginx
Add inside your WordPress `server { ... }` block (`/etc/nginx/sites-available/...`):
```nginx
location /ward-api/ {
    proxy_pass http://127.0.0.1:8000/api/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```
Reload Nginx:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

### If Apache
Enable proxy modules and add rules to your WordPress `<VirtualHost>` config (`/etc/apache2/sites-available/...`):
```bash
sudo a2enmod proxy proxy_http
```
```apache
ProxyPass /ward-api/ http://127.0.0.1:8000/api/
ProxyPassReverse /ward-api/ http://127.0.0.1:8000/api/
```
Reload Apache:
```bash
sudo apache2ctl configtest && sudo systemctl reload apache2
```

Verify via browser or `curl`: `https://yourdomain.com/ward-api/health`

---

## 7. Run 24/7 with `systemd` & Gunicorn
Create `/etc/systemd/system/camas-ward-api.service` (adjust `User` and paths if your username is not `pi`):

```ini
[Unit]
Description=Camas Ward Lookup Flask API
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/camas-ward-api
ExecStart=/home/pi/camas-ward-api/venv/bin/gunicorn --workers 1 --threads 2 --bind 127.0.0.1:8000 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now camas-ward-api
sudo systemctl status camas-ward-api
```

---

## 8. WordPress Frontend Integration
Add a **Custom HTML** block on a WordPress page:

```html
<div id="camas-ward-lookup" style="max-width: 520px;">
  <form id="ward-form">
    <label for="ward-address"><strong>Enter your Camas street address:</strong></label><br>
    <input
      id="ward-address"
      type="text"
      placeholder="e.g., 616 NE 4th Ave"
      maxlength="200"
      required
      style="width: 100%; padding: 8px; margin: 8px 0;"
    />
    <button type="submit" style="padding: 8px 16px;">Find My Councilors</button>
  </form>
  <div id="ward-result" style="margin-top: 16px;"></div>
</div>

<script>
document.getElementById("ward-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const address = document.getElementById("ward-address").value.trim();
  const resultDiv = document.getElementById("ward-result");
  resultDiv.textContent = "Looking up address...";

  try {
    const resp = await fetch("/ward-api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address }),
    });
    const data = await resp.json();

    if (!resp.ok) {
      resultDiv.textContent = data.error || "Unable to find ward for that address.";
      return;
    }

    resultDiv.replaceChildren();

    const matchedP = document.createElement("p");
    const matchedLabel = document.createElement("strong");
    matchedLabel.textContent = "Matched address: ";
    matchedP.append(matchedLabel, document.createTextNode(data.matched_address));
    resultDiv.appendChild(matchedP);

    if (data.on_boundary) {
      const noteP = document.createElement("p");
      const noteEm = document.createElement("em");
      noteEm.textContent = "Note: This geocoded point sits directly on a ward boundary.";
      noteP.appendChild(noteEm);
      resultDiv.appendChild(noteP);
    }

    const ul = document.createElement("ul");
    for (const w of data.wards) {
      const li = document.createElement("li");
      const wardStrong = document.createElement("strong");
      wardStrong.textContent = `${w.ward}: `;
      li.append(wardStrong, document.createTextNode(w.councilors));
      ul.appendChild(li);
    }
    resultDiv.appendChild(ul);

    const atLargeP = document.createElement("p");
    const atLargeLabel = document.createElement("strong");
    atLargeLabel.textContent = "At-Large Councilor: ";
    atLargeP.append(atLargeLabel, document.createTextNode(data.at_large));
    resultDiv.appendChild(atLargeP);
  } catch (err) {
    resultDiv.textContent = "Network error contacting the lookup service.";
  }
});
</script>
```

---

## 9. Maintenance Checklist
- **Contact User-Agent:** Replace `contact@yourdomain.com` in `ward_lookup.py` and `app.py` with a real email address before going live.
- **After Elections:** Update `Data/councilors.json` and restart the service (`sudo systemctl restart camas-ward-api`).
- **After City Annexations:** Replace `Data/ward_boundaries.geojson` with updated boundary data and restart the service.