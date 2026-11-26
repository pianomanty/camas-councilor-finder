import json
from functools import partial
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import usaddress
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from probableparsing import RepeatedLabelError

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
    user_agent: str = "camas_ward_lookup_cli/1.0",
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


def main() -> None:
    user_address = input("Please enter your address: ")
    geocode_fn = build_geocoder()

    try:
        address_gdf = geocode_address(user_address, geocode_fn)
        ward_data = load_ward_data()
        matches = find_wards(address_gdf, ward_data)
    except (ValueError, FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}")
        return

    matched_address = matches["matched_address"].iloc[0]
    at_large = matches["At_Large"].iloc[0]

    print(f"Matched address: {matched_address}")

    if len(matches) > 1:
        print(
            "Note: This geocoded point sits directly on a ward boundary (street-level "
            "geocoding can sometimes place a pin on a dividing street):"
        )
        for _, row in matches.iterrows():
            print(f"  - {row['Ward_Cat']}: {row['Councilors']}")
    else:
        row = matches.iloc[0]
        print(f"You live in {row['Ward_Cat']}")
        print(f"Your councilors are {row['Councilors']}")

    print(f"You are also represented by at-large councilor {at_large}")


if __name__ == "__main__":
    main()