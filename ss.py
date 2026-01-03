import requests
import json

response = requests.get(
  url="https://openrouter.ai/api/v1/key",
  headers={
    "Authorization": f"Bearer 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"
  }
)

print(json.dumps(response.json(), indent=2))
