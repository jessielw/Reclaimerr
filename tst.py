import asyncio

from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.services.radarr import RadarrClient
from backend.services.sonarr import SonarrClient

# from backend.services.seerr import SeerrClient

# seerr = SeerrClient(
#     api_key="MTc0MzY2MzIyMDEyNTk5NWNjM2Q3LWZmYmQtNDdjZS1hNzc4LWQ0ODYzODg2NmQ3Zg==",
#     base_url="https://request.jessielw.com",
# )

# results = asyncio.run(seerr.get_all_users())


# import asyncio
# from backend.database import async_db
# from backend.services.admin_notices import create_event_notice

# async def main():
#     async with async_db() as db:
#         await create_event_notice(
#             db,
#             kind="event_admin_message",
#             severity="warning",
#             title="Test Notice",
#             message="This is a simulated admin notice.",
#             action_label="Go to Settings",
#             action_href="#/settings",
#             context_json={"source": "manual_test"},
#         )
#         await db.commit()
#     print("ok")

# asyncio.run(main())


# radarr = RadarrClient(
#     api_key="d731fa1094ad4e9896358c97630bc6ea",
#     base_url="https://radarr.jessielw.com",
# )

# print(asyncio.run(radarr.get_import_list_tmdb_map()))


# sonarr = SonarrClient(
#     api_key="4f506837bcbb4f5c8947b36d169ba2bd",
#     base_url="https://sonarr.jessielw.com",
# )

# test = asyncio.run(sonarr.get_all_series())
# # print(test)

# plex = PlexService(
#     token="AeheFej2GFMsasCK4ZhH",
#     plex_url="http://plex.jessielw.com",
# )

jf = JellyfinService(
    api_key="20457acfc87a4e5a9a3595374f7ad9c0",
    base_url="https://jellyfin.jessielw.com",
)

print(asyncio.run(jf.get_favorite_tmdb_ids_by_user("series")))

