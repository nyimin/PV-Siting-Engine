import requests
import os
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("PVWATTS_API_KEY", "DEMO_KEY")
lat, lon = 19.986376, 95.259394
url = f"https://developer.nrel.gov/api/solar/data_query/v1.json?api_key={api_key}&lat={lat}&lon={lon}"

try:
    resp = requests.get(url).json()
    outputs = resp.get("outputs", {})
    for k, v in outputs.items():
        print(f"Dataset type: {k}")
except Exception as e:
    print("Error querying NREL:", type(e), e)
