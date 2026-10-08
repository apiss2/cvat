"""Exercise the CVAT session API's Django and DRF compatibility contract.

This is a small independent Django app, not an imported CVAT installation.
It reproduces the Host and Accept requirements hidden by mock user responses.
"""

import asyncio

import django
import httpx
import pytest
from django.conf import settings as django_settings
from django.test import Client, override_settings
from fastapi.testclient import TestClient

from conftest import FakeRuntime, TOKENS
from registry.config import Settings
from registry.manager import create_app


@pytest.fixture(scope="module")
def django_server(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("cvat-session-db")
    django_settings.configure(
        SECRET_KEY="only-for-the-isolated-test-database",
        ALLOWED_HOSTS=["cvat.example"],
        ROOT_URLCONF=__name__,
        USE_X_FORWARDED_HOST=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "rest_framework",
        ],
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": str(db_dir / "db.sqlite3"),
            }
        },
        # CVAT uses AcceptHeaderVersioning for its versioned API responses.
        REST_FRAMEWORK={
            "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.AcceptHeaderVersioning",
            # Keep the isolated app aligned with CVAT's supported API version.
            "ALLOWED_VERSIONS": "2.0",
            "DEFAULT_VERSION": "2.0",
            "VERSION_PARAM": "version",
        },
        LOGGING_CONFIG=None,
    )
    django.setup()
    from django.contrib.auth.models import User
    from django.core.management import call_command
    from django.urls import path
    from rest_framework import serializers
    from rest_framework.authentication import SessionAuthentication, TokenAuthentication
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer
    from rest_framework.response import Response
    from rest_framework.views import APIView

    class CVATJSONRenderer(JSONRenderer):
        # Match CVAT's public API media type.
        # The plain application/json renderer is not enabled in CVAT settings.
        media_type = "application/vnd.cvat+json"

    class SelfSerializer(serializers.ModelSerializer):
        groups = serializers.SlugRelatedField(
            many=True, slug_field="name", read_only=True
        )

        class Meta:
            model = User
            # A ModelSerializer with a url field builds an absolute URL via
            # request.get_host(), as CVAT's UserSerializer does for /users/self.
            fields = ("url", "id", "username", "groups", "is_active", "is_superuser")

    class SelfView(APIView):
        authentication_classes = [TokenAuthentication, SessionAuthentication]
        permission_classes = [IsAuthenticated]
        renderer_classes = [CVATJSONRenderer, BrowsableAPIRenderer]

        def get(self, request, **kwargs):
            return Response(
                SelfSerializer(request.user, context={"request": request}).data
            )

    global urlpatterns
    urlpatterns = [
        path("api/users/self", SelfView.as_view()),
        path("api/users/<int:pk>", SelfView.as_view(), name="user-detail"),
    ]
    call_command("migrate", verbosity=0)
    account = User.objects.create_user(username="alice")
    client = Client(raise_request_exception=False)
    client.force_login(account)
    yield client, account
    from django.db import connections

    connections.close_all()


def test_cvat_rejects_plain_json_despite_valid_session(django_server):
    client, _ = django_server
    response = client.get(
        "/api/users/self", HTTP_HOST="cvat.example", HTTP_ACCEPT="application/json"
    )
    assert response.status_code == 406


@pytest.mark.parametrize(
    "accept",
    [
        "application/vnd.cvat+json",
        "application/vnd.cvat+json; version=2.0",
    ],
)
def test_cvat_vendor_json_accepts_default_or_explicit_api_version(
    django_server, accept
):
    client, _ = django_server
    response = client.get(
        "/api/users/self", HTTP_HOST="cvat.example", HTTP_ACCEPT=accept
    )
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/vnd.cvat+json")
    assert response.json()["username"] == "alice"
    assert response.renderer_context["request"].version == "2.0"


def test_compose_service_hostname_fails_real_django_even_with_wildcard_allowed_hosts(
    django_server,
):
    client, _ = django_server
    with override_settings(ALLOWED_HOSTS=["*"]):
        # Existing session is valid; only the internal hostname is different.
        invalid_host = client.get(
            "/api/users/self",
            HTTP_HOST="cvat_server:8080",
            HTTP_ACCEPT="application/vnd.cvat+json",
        )
        valid_host = client.get(
            "/api/users/self",
            HTTP_HOST="cvat.example",
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_ACCEPT="application/vnd.cvat+json",
        )
    assert invalid_host.status_code == 400
    assert valid_host.status_code == 200
    assert valid_host.json()["url"].startswith("https://cvat.example/api/users/")
    assert valid_host.json()["username"] == "alice"


def test_registry_reuses_real_django_session_through_internal_connection(
    django_server, tmp_path, monkeypatch
):
    django_client, account = django_server
    session = django_client.cookies["sessionid"].value
    secret = tmp_path / "service_token"
    secret.write_text(TOKENS["service"])
    settings = Settings(
        data_dir=tmp_path / "data",
        host_data_dir=str(tmp_path / "data"),
        service_token_file=secret,
        public_url="https://cvat.example/model-registry/",
    )
    calls = []

    async def django_transport(request):
        calls.append(request)
        assert request.url.host == "cvat_server"
        assert request.headers["host"] == "cvat.example"
        assert request.headers["x-forwarded-proto"] == "https"

        def handle_request():
            response = django_client.get(
                request.url.path,
                **{
                    "HTTP_" + key.upper().replace("-", "_"): value
                    for key, value in request.headers.items()
                },
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"Content-Type": response["Content-Type"]},
            )

        # Django DB access stays in a synchronous thread, as in its WSGI server.
        return await asyncio.to_thread(handle_request)

    actual_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: actual_client(
            transport=httpx.MockTransport(django_transport), **kwargs
        ),
    )
    with TestClient(
        create_app(settings, FakeRuntime()), base_url="https://cvat.example"
    ) as client:
        client.cookies.set("sessionid", session, path="/")
        response = client.get(
            "/api/me",
            headers={
                "Host": "untrusted.example",
                "X-Forwarded-Host": "untrusted.example",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "id": account.pk,
            "name": "alice",
            "admin": False,
            "auth_mode": "cvat",
        }
        assert client.get("/api/models").status_code == 200
        assert len(calls) == 2
        client.cookies.set("sessionid", "expired-session-value", path="/")
        assert client.get("/api/me").status_code == 401
