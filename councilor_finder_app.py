import geopandas as gpd
import pandas as pd
from matplotlib import pyplot as plt
from pyproj import CRS
from os import getcwd
from pathlib import Path
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import usaddress
from os import getcwd

# Address cleaning
def clean_us_address(raw_address):
    """
    Parses messy US addresses, strips unit/apartment numbers (which throw off geocoders),
    and reconstructs a standardized string: 'Street Number Street Name, City, State Zip'
    """
    if not isinstance(raw_address, str) or not raw_address.strip():
        return None
    
    try:
        # Tag the components using Conditional Random Fields
        tagged_dict, address_type = usaddress.tag(raw_address)
    except usaddress.RepeatedLabelError:
        # Fallback if the address contains repeating ambiguous words
        return raw_address.strip()
    
    # Extract key parts safely
    num = tagged_dict.get('AddressNumber', '')
    predir = tagged_dict.get('StreetNamePreDirectional', '')
    street = tagged_dict.get('StreetName', '')
    postdir = tagged_dict.get('StreetNamePostDirectional', '')
    st_type = tagged_dict.get('StreetNamePostType', '')
    city = tagged_dict.get('PlaceName', '')
    state = tagged_dict.get('StateName', '')
    zip_code = tagged_dict.get('ZipCode', '')
    
    # Assemble street line (skipping Apt/Suite numbers as geocoders match better at street level)
    street_parts = [num, predir, street, st_type, postdir]
    street_line = " ".join([p for p in street_parts if p]).strip()
    
    # Assemble standard format
    city_state_zip = f"{city}, {state} {zip_code}".strip(", ")
    
    if street_line and city_state_zip:
        return f"{street_line}, {city_state_zip}"
    elif street_line:
        return street_line
    return raw_address.strip()

def geolocate_address(addresses):
    df = pd.DataFrame({"raw_input": addresses})
    df["clean_address"] = df["raw_input"].apply(clean_us_address)

    # Initialize free OpenStreetMap Nominatim geocoder
    # Note: Always pass a unique `user_agent` string per Nominatim usage guidelines
    geolocator = Nominatim(user_agent="my_us_address_cleaner_app")

    # Wrap with RateLimiter to adhere to Nominatim's 1-request-per-second rule
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.0)

    # Perform geocoding
    df["location"] = df["clean_address"].apply(geocode)

    # Extract coordinates and full returned address
    df["latitude"] = df["location"].apply(lambda loc: loc.latitude if loc else None)
    df["longitude"] = df["location"].apply(lambda loc: loc.longitude if loc else None)

    # -------------------------------------------------------------
    # 4. Convert to GeoPandas DataFrame
    # -------------------------------------------------------------
    gdf_addresses = gpd.GeoDataFrame(
        df, 
        geometry=gpd.points_from_xy(df.longitude, df.latitude), 
        crs="EPSG:4326"
    )
    return gdf_addresses

def load_ward_data():
    # Define data path, import ward data
    data_path = Path('/Users/tyler/Documents/Repositories/GIS_Analysis/Camas Ward Info/Data/ward_boundaries.geojson')
    ward_data = gpd.read_file(data_path)
    ward_data = ward_data.dropna() # investigation showed the NaN row was some islands in Lacamas lake lol
    crs_camas = ward_data.crs

    # Create categorical data for ward names
    ward_data['Ward_Cat'] = ward_data['Ward'].astype(int).astype(str)
    ward_data['Ward_Cat'] = 'Ward ' + ward_data['Ward_Cat']  # Optional: Label them nicely ("Ward 1", etc.)

    # Make councilors dictionary
    ward_councilors = {
        1: ["Marilyn Boerke", "John Nohr"],
        2: ["Martin Elzingre", "Tim Hein"],
        3: ["Jennifer Senescu", "Mahsa Eshghi"],
    }

    # Add councilors to dataframe
    ward_data['Councilors'] = ward_data['Ward'].map(ward_councilors).apply(
        lambda names: " & ".join(names) if isinstance(names, list) else names
    )

    # Add the At-Large representative separate column
    at_large_member = "John Svilarich"
    ward_data['At_Large'] = at_large_member

    return ward_data

def find_wards(address_df,ward_data):
    # 1. Ensure both DataFrames use the exact same CRS
    if address_df.crs != ward_data.crs:
        address_df = address_df.to_crs(ward_data.crs)

    # 2. Spatial join with gdf_addresses on the left (keeps all addresses)
    ward_selection = gpd.sjoin(
        address_df,                                    # Left side: keeps ALL addresses
        ward_data[['Ward_Cat', 'Councilors', 'At_Large', 'geometry']], # Right side: ward columns & geometry to bring over
        how='left',                                       # Keep every address row even if it misses a ward
        predicate='within'                              # Check if the address point lies within the ward polygon
    )

    # Optional: Clean up duplicate/extra index columns created during the join
    ward_selection = ward_selection.drop(columns=['index_right'], errors='ignore')

    #Raise error if nothing found
    has_misses = ward_selection["Ward_Cat"].isna().any()
    if has_misses:
        raise ValueError('No match found')
    return ward_selection

if __name__ == '__main__':
    print(getcwd())
    # Get user address, clean it, geolocate it
    user_address = input('Please enter your address: ')
    cleaned_address = clean_us_address(user_address)
    address_geolocated_df = geolocate_address([cleaned_address])

    # Load data
    ward_data = load_ward_data()

    try:
        # Find ward info
        result = find_wards(address_geolocated_df,ward_data)
        # Extract ward info and councilors
        ward_info = result["Ward_Cat"].iloc[0]
        ward_councilors = result["Councilors"].iloc[0]
        at_large = result["At_Large"].iloc[0]

        print(f'You live in {ward_info}')
        print(f'Your councilors are {ward_councilors}')
        print(f'You are also represented by at-large councilor {at_large}')
    except ValueError as e:
        print(e)

