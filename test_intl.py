import os
from dotenv import load_dotenv
import PySAM.ResourceTools as rt
import logging

logging.basicConfig(level=logging.INFO)
load_dotenv()

api_key = os.environ.get("PVWATTS_API_KEY")

try:
    fetcher = rt.FetchResourceFiles(
        tech="solar",
        workers=1,
        resource_type="intl",
        resource_year="tmy",
        resource_interval_min=60,
        nrel_api_key=api_key,
        nrel_api_email="nyimin.sg@gmail.com",
    )
    fetcher.fetch([(19.986376, 95.259394)])
    print("Success. Path:", fetcher.resource_file_paths)
except Exception as e:
    print("Error:", type(e), e)
