import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import usaddress
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from probableparsing import RepeatedLabelError

DATA_DIR = Path(__file__).resolve().parent / "Data"
DEFAULT_WARD_GEOJSON = DATA_DIR / "ward_boundaries.geojson"
DEFAULT_COUNCILORS_JSON = DATA_DIR / "councilors.json"


def clean_us_address(raw_address: str | None) -> str | None:
    """Parse a US address and strip unit/apartment numbers for street-level geocoding."""
    if not isinstance(raw_address, str) or not raw_address.strip():
        return None

    try:
        tagged_dict, _ = usaddress.tag(raw_address)
    except RepeatedLabelError:
        return raw_address.strip()
    
    street_parts = [
        tagged_dict.get("AddressNumber", ""),
        tagged_dict.get("StreetNamePreDirectional", ""),
        tagged_dict.get("StreetName", ""),
        tagged_dict.get("StreetNamePostType", ""),
        tagged_dict.get("StreetNamePostDirectional", ""),
    ]
    street_line = " ".join(part for part in street_parts if part).strip()

    city = tagged_dict.get("PlaceName", "")
    state = tagged_dict.get("StateName", "")
    zip_code = tagged_dict.get("ZipCode", "")
    city_state_zip = f"{city}, {state} {zip_code}".strip(", ")

    if street_line and city_state_zip:
        return f"{street_line}, {city_state_zip}"
    if street_line:
        return street_line
    return raw_address.strip()


def build_geocoder(user_agent: str = "camas_ward_lookup_cli"):
    """Initialize a rate-limited Nominatim geocoder (1 req/sec per usage policy)."""
    geolocator = Nominatim(user_agent=user_agent)
    return RateLimiter(geolocator.geocode, min_delay_seconds=1.0)


def geocode_address(raw_address: str, geocode_fn) -> gpd.GeoDataFrame:
    """Clean and geocode a single address into a single-row GeoDataFrame (EPSG:4326)."""
    cleaned = clean_us_address(raw_address)
    if not cleaned:
        raise ValueError("Please enter a valid non-empty address.")

    location = geocode_fn(cleaned)
    if location is None:
        raise ValueError(f"Could not geocode address: '{cleaned}'")

    df = pd.DataFrame(
        [
            {
                "raw_input": raw_address.strip(),
                "clean_address": cleaned,
                "matched_address": location.address,
                "latitude": location.latitude,
                "longitude": location.longitude,
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


def find_ward(address_gdf: gpd.GeoDataFrame, ward_data: gpd.GeoDataFrame) -> pd.Series:
    """Return the matching ward row for a point GeoDataFrame, or raise ValueError."""
    if address_gdf.crs != ward_data.crs and ward_data.crs is not None:
        address_gdf = address_gdf.to_crs(ward_data.crs)

    joined = gpd.sjoin(
        address_gdf,
        ward_data[["Ward_Cat", "Councilors", "At_Large", "geometry"]],
        how="left",
        predicate="intersects",
    ).drop(columns=["index_right"], errors="ignore")

    match = joined.iloc[0]
    if pd.isna(match["Ward_Cat"]):
        raise ValueError("Address does not fall within a Camas city ward.")

    return match


def main() -> None:
    user_address = input("Please enter your address: ")
    geocode_fn = build_geocoder()

    try:
        address_gdf = geocode_address(user_address, geocode_fn)
        ward_data = load_ward_data()
        match = find_ward(address_gdf, ward_data)
    except (ValueError, FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}")
        return

    print(f"Matched address: {match['matched_address']}")
    print(f"You live in {match['Ward_Cat']}")
    print(f"Your councilors are {match['Councilors']}")
    print(f"You are also represented by at-large councilor {match['At_Large']}")


if __name__ == "__main__":
    main()