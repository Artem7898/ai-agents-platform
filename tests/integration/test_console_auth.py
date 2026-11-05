"""Browser sessions do not weaken token APIs or put credentials in JavaScript."""

import json
import uuid
from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, RequestFactory
from rest_framework.authtoken.models import Token

from src.agents.models import Agent
from src.billing.models import Invoice, PlanVersion
from src.billing.services import record_payment, start_subscription
from src.console.auth import _MAX_BODY, _body

pytestmark = pytest.mark.django_db(transaction=True)
PASSWORD = "Correct-console-password-42!"


@pytest.fixture(autouse=True)
def clear_login_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username="console-user", password=PASSWORD)


def bootstrap(client):
    response = client.get("/console/api/session/")
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    return response.json()["csrf_token"]


def post(client, path, data, csrf):
    return client.post(
        path, data=json.dumps(data), content_type="application/json", HTTP_X_CSRFTOKEN=csrf
    )


def test_browser_login_rotates_csrf_uses_httponly_session_and_mints_no_api_token(account):
    client = Client(enforce_csrf_checks=True)
    csrf = bootstrap(client)
    assert client.get("/console/api/session/").json()["authenticated"] is False
    assert (
        post(
            client, "/console/api/login/", {"username": account.username, "password": PASSWORD}, ""
        ).status_code
        == 403
    )
    response = post(
        client, "/console/api/login/", {"username": account.username, "password": PASSWORD}, csrf
    )
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] and body["user"]["id"] == account.pk
    assert response.cookies["sessionid"]["httponly"]
    assert not Token.objects.filter(user=account).exists()
    assert PASSWORD not in response.content.decode()
    assert "token" not in body["user"]
    # API clients continue to need a real API Token even with a console session.
    assert client.get("/api/v1/agents/").status_code == 401
    assert client.get("/console/api/agents/").status_code == 200
    assert post(client, "/console/api/logout/", {}, csrf).status_code == 403
    assert post(client, "/console/api/logout/", {}, body["csrf_token"]).status_code == 200
    assert client.get("/console/api/agents/").status_code == 401


def test_login_rejects_cross_origin_even_with_a_valid_csrf_cookie(account):
    client = Client(enforce_csrf_checks=True)
    csrf = bootstrap(client)
    response = client.post(
        "/console/api/login/",
        data=json.dumps({"username": account.username, "password": PASSWORD}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
        HTTP_ORIGIN="https://untrusted.example",
    )
    assert response.status_code == 403
    assert client.get("/console/api/session/").json()["authenticated"] is False


def test_invalid_credentials_and_inactive_users_have_the_same_safe_response(account):
    client = Client(enforce_csrf_checks=True)
    csrf = bootstrap(client)
    invalid = post(
        client, "/console/api/login/", {"username": account.username, "password": "wrong"}, csrf
    )
    account.is_active = False
    account.save(update_fields=["is_active"])
    inactive = post(
        client, "/console/api/login/", {"username": account.username, "password": PASSWORD}, csrf
    )
    assert invalid.status_code == inactive.status_code == 401
    assert invalid.json() == inactive.json() == {"code": "invalid_credentials"}
    assert not Token.objects.exists()


def test_login_attempts_are_limited_and_sensitive_values_are_not_returned(account):
    client = Client(enforce_csrf_checks=True)
    csrf = bootstrap(client)
    for _ in range(10):
        assert (
            post(
                client,
                "/console/api/login/",
                {"username": account.username, "password": "wrong"},
                csrf,
            ).status_code
            == 401
        )
    response = post(
        client, "/console/api/login/", {"username": account.username, "password": PASSWORD}, csrf
    )
    assert response.status_code == 429
    assert response.json() == {"code": "login_rate_limited"}
    assert response["Retry-After"] == "300"
    assert PASSWORD not in response.content.decode()


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "console-user", "password": PASSWORD, "is_staff": True},
        {"username": ["console-user"], "password": PASSWORD},
        {"username": "console-user", "password": "x" * 1025},
    ],
)
def test_login_rejects_unexpected_fields_and_unbounded_values(account, payload):
    client = Client(enforce_csrf_checks=True)
    response = post(client, "/console/api/login/", payload, bootstrap(client))
    assert response.status_code == 400 and response.json() == {"code": "invalid_login"}


def test_failed_throttle_backend_cannot_enable_unthrottled_authentication(account, monkeypatch):
    client = Client(enforce_csrf_checks=True)
    csrf = bootstrap(client)

    def unavailable(*args, **kwargs):
        raise RuntimeError("sensitive cache diagnostic")

    monkeypatch.setattr("src.console.auth._increment", unavailable)
    response = post(
        client, "/console/api/login/", {"username": account.username, "password": PASSWORD}, csrf
    )
    assert response.status_code == 503
    assert response.json() == {"code": "authentication_unavailable"}
    assert client.get("/console/api/session/").json()["authenticated"] is False


def test_missing_content_length_never_reads_or_allocates_the_entire_login_body():
    class BoundedStream(BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.requested = []

        def read(self, size=-1):
            assert size == _MAX_BODY + 1
            self.requested.append(size)
            return super().read(size)

    request = RequestFactory().post("/console/api/login/", content_type="application/json")
    request.META.pop("CONTENT_LENGTH", None)
    stream = BoundedStream(b" " * (_MAX_BODY + 50_000))
    request._stream = stream
    with pytest.raises(ValueError):
        _body(request, ("username", "password"))
    assert stream.requested == [_MAX_BODY + 1]
    assert stream.tell() == _MAX_BODY + 1
    assert not hasattr(request, "_body")


def test_deep_json_inside_the_body_limit_is_a_safe_login_error():
    client = Client(enforce_csrf_checks=True)
    csrf = bootstrap(client)
    raw = '{"username":' + "[" * 3000 + '"nested"' + "]" * 3000 + ',"password":"private"}'
    assert len(raw) < _MAX_BODY
    response = client.post(
        "/console/api/login/", data=raw, content_type="application/json", HTTP_X_CSRFTOKEN=csrf
    )
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_login"}
    assert not Token.objects.exists()


def test_resource_mirror_preserves_owner_permissions_and_csrf(account):
    other = get_user_model().objects.create_user(username="other-owner")
    private = Agent.objects.create(
        owner=other, name="Private agent", model_name="model", system_prompt="private"
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(account)
    csrf = bootstrap(client)
    assert client.get(f"/console/api/agents/{private.pk}/").status_code == 404
    body = {
        "name": "Browser agent",
        "model_name": "model",
        "system_prompt": "Brief answers",
        "owner": other.pk,
    }
    assert post(client, "/console/api/agents/", body, "").status_code == 403
    response = post(client, "/console/api/agents/", body, csrf)
    assert response.status_code == 201 and response.json()["owner"] == account.pk
    rows = client.get("/console/api/agents/?limit=100000").json()
    assert rows["count"] == 1 and rows["results"][0]["name"] == "Browser agent"
    assert "no-store" in client.get("/console/api/agents/")["Cache-Control"]
    assert not Token.objects.filter(user=account).exists()


def test_profile_and_password_updates_remain_own_session_only(account):
    client = Client(enforce_csrf_checks=True)
    client.force_login(account)
    other_session = Client()
    other_session.force_login(account)
    csrf = bootstrap(client)
    assert post(client, "/console/api/profile/", {"is_staff": True}, csrf).status_code == 400
    response = post(
        client, "/console/api/profile/", {"first_name": "Artem", "last_name": "Example"}, csrf
    )
    assert response.status_code == 200 and response.json()["user"]["first_name"] == "Artem"
    assert (
        post(
            client,
            "/console/api/password/",
            {"old_password": "wrong", "new_password": "New-safe-console-79!"},
            csrf,
        ).status_code
        == 400
    )
    response = post(
        client,
        "/console/api/password/",
        {"old_password": PASSWORD, "new_password": "New-safe-console-79!"},
        csrf,
    )
    assert response.status_code == 200
    assert client.get("/console/api/session/").json()["authenticated"]
    assert other_session.get("/console/api/session/").json()["authenticated"] is False
    account.refresh_from_db()
    assert account.check_password("New-safe-console-79!")
    assert not account.is_staff


def test_console_staff_only_resources_do_not_gain_permissions_from_session(account):
    client = Client(enforce_csrf_checks=True)
    client.force_login(account)
    csrf = bootstrap(client)
    assert client.get("/console/api/operations/").status_code == 403
    response = post(client, "/console/api/billing-plans/", {"name": "Fake"}, csrf)
    assert response.status_code == 403
    assert client.get("/console/api/tools/").status_code == 200
    assert (
        client.post("/console/api/execute/", data="{}", content_type="application/json").status_code
        == 403
    )
    assert client.get(f"/console/api/agents/{uuid.uuid4()}/").status_code == 404


def test_staff_personal_billing_scope_filters_both_owner_and_mode(account, settings):
    settings.BILLING_ENABLED = True
    account.is_staff = True
    account.save(update_fields=["is_staff"])
    other = get_user_model().objects.create_user(username="separate-billing-owner")
    plan = PlanVersion.objects.create(
        code="browser-plan",
        version="1",
        name="Monthly",
        price_minor=100,
        currency="USD",
        included_tokens=1000,
        included_requests=10,
        overage_per_million_tokens_minor=1,
        overage_per_request_minor=1,
    )
    current = None
    for owner, mode in ((account, "test"), (other, "test"), (account, "live")):
        settings.BILLING_MODE = mode
        period = start_subscription(owner_id=owner.pk, plan_id=plan.pk)
        invoice = Invoice.objects.get(period=period, kind="base")
        receipt = record_payment(
            invoice_id=invoice.pk,
            actor_id=account.pk,
            provider="manual",
            merchant_account="test-merchant",
            external_payment_id=str(period.pk),
            amount_minor=100,
            currency="USD",
            evidence_ref="verified-reference",
        )
        if owner.pk == account.pk and mode == "test":
            current = {
                "billing-subscriptions": period.subscription_id,
                "billing-periods": period.pk,
                "billing-invoices": invoice.pk,
                "billing-payments": receipt.pk,
            }
    settings.BILLING_MODE = "test"
    client = Client()
    client.force_login(account)
    assert current is not None
    for endpoint, identifier in current.items():
        assert client.get(f"/console/api/{endpoint}/").json()["count"] == 3
        own = client.get(f"/console/api/{endpoint}/?scope=mine").json()
        assert own["count"] == 1 and own["results"][0]["id"] == str(identifier)
