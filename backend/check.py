import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

base_url = os.getenv("USDA_MARS_BASE_URL", "https://marsapi.ams.usda.gov/services/v1.2")
api_key = os.getenv("USDA_MMN_API_KEY")

resp = requests.get(
    f"{base_url}/reports",
    auth=HTTPBasicAuth(api_key, ""),
    timeout=30,
)

print(resp.status_code)
print(resp.text[:500])