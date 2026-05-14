import asyncio

from backend.services.seerr import SeerrClient

seerr = SeerrClient(
    api_key="MTc0MzY2MzIyMDEyNTk5NWNjM2Q3LWZmYmQtNDdjZS1hNzc4LWQ0ODYzODg2NmQ3Zg==",
    base_url="https://request.jessielw.com",
)

results = asyncio.run(seerr.get_requests())

# print(len(results))
# for result in results:
#     print(len(result))
