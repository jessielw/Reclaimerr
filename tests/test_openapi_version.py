from fastapi.testclient import TestClient

from backend.api.main import fastapi_app
from backend.core.__version__ import __version__


def test_root_openapi_version_matches_application_version() -> None:
    # Avoid starting background workers or connecting to the application database.
    client = TestClient(fastapi_app)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["version"] == str(__version__)
