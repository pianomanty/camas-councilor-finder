from threading import Lock

from flask import Flask, jsonify, request
from ward_lookup import (
    build_geocoder,
    find_wards,
    geocode_address,
    load_ward_data,
)

app = Flask(__name__)

# Load ward polygons & councilors once when the server starts
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