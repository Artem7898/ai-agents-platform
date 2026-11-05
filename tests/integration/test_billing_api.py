"""Billing APIs bind owners, consent and payment evidence on the server."""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from src.billing import services
from src.billing.contracts import MAX_MINOR
from src.billing.models import BillingPeriod, Invoice, PaymentReceipt, PlanVersion, Subscription

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def identities(settings):
    settings.BILLING_ENABLED = True
    settings.BILLING_MODE = "test"
    users = {}
    for name in ("alice", "bob", "staff"):
        user = get_user_model().objects.create_user(
            username=f"billing-{name}-{uuid.uuid4().hex}", is_staff=name == "staff"
        )
        token = Token.objects.create(user=user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        users[name] = user, client
    return users


def plan_body(**values):
    return {
        "code": "team",
        "version": "2026-11",
        "name": "Team monthly",
        "price_minor": 1000,
        "currency": "USD",
        "included_tokens": 1000,
        "included_requests": 10,
        "overage_per_million_tokens_minor": 5000,
        "overage_per_request_minor": 2,
        "enabled": True,
        **values,
    }


@pytest.fixture
def plan():
    return PlanVersion.objects.create(**plan_body())


def start(client, plan, cap=100):
    response = client.post(
        "/api/v1/billing-subscriptions/",
        {"plan_id": str(plan.pk), "overage_limit_minor": cap},
        format="json",
    )
    assert response.status_code == 201, response.data
    return response.json()


def evidence(**values):
    return {
        "provider": "manual-bank",
        "merchant_account": "merchant-test",
        "external_payment_id": "payment-" + uuid.uuid4().hex,
        "amount_minor": 1000,
        "currency": "USD",
        "evidence_ref": "statement:2026-11:line-1",
        **values,
    }


def pay(client, invoice_id, **values):
    return client.post(
        f"/api/v1/billing-invoices/{invoice_id}/record-payment/",
        evidence(**values),
        format="json",
    )


def test_billing_endpoints_require_token(identities):
    anonymous = APIClient()
    for endpoint in (
        "billing-plans",
        "billing-subscriptions",
        "billing-periods",
        "billing-invoices",
        "billing-payments",
    ):
        assert anonymous.get(f"/api/v1/{endpoint}/").status_code == 401


def test_staff_can_configure_immutable_plan_versions_before_enabling_billing(identities, settings):
    _, alice = identities["alice"]
    _, staff = identities["staff"]
    settings.BILLING_ENABLED = False
    assert alice.post("/api/v1/billing-plans/", plan_body(), format="json").status_code == 403
    response = staff.post("/api/v1/billing-plans/", plan_body(), format="json")
    assert response.status_code == 201, response.data
    url = f"/api/v1/billing-plans/{response.json()['id']}/"
    assert staff.patch(url, {"enabled": False}, format="json").status_code == 200
    for field, value in (("price_minor", 1), ("currency", "EUR"), ("included_tokens", 9999)):
        assert staff.patch(url, {field: value}, format="json").status_code == 400
    assert staff.put(url, plan_body(), format="json").status_code == 405
    assert staff.delete(url).status_code == 405
    stored = PlanVersion.objects.get(pk=response.json()["id"])
    assert stored.price_minor == 1000 and stored.included_tokens == 1000
    assert not stored.enabled


@pytest.mark.parametrize("value", [True, 1000.0, "1000", -1, MAX_MINOR + 1])
def test_money_inputs_require_bounded_json_integers(identities, value):
    _, staff = identities["staff"]
    response = staff.post("/api/v1/billing-plans/", plan_body(price_minor=value), format="json")
    assert response.status_code == 400
    assert not PlanVersion.objects.exists()


def test_start_binds_owner_and_creates_only_unpaid_entitlement(identities, plan):
    owner, alice = identities["alice"]
    other, bob = identities["bob"]
    _, staff = identities["staff"]
    for unwanted in (
        {"owner": other.pk},
        {"status": "active"},
        {"amount_minor": 0},
        {"paid_at": "2026-11-01T00:00:00Z"},
    ):
        response = alice.post(
            "/api/v1/billing-subscriptions/",
            {"plan_id": str(plan.pk), **unwanted},
            format="json",
        )
        assert response.status_code == 400
    assert not Subscription.objects.exists()

    period = start(alice, plan, cap=123)
    subscription = Subscription.objects.get(pk=period["subscription"])
    assert subscription.account.owner_id == owner.pk
    assert subscription.status == "pending"
    assert period["paid_at"] is None and period["overage_limit_minor"] == 123
    assert period["invoices"][0]["status"] == "open"
    assert period["invoices"][0]["amount_minor"] == plan.price_minor
    assert not PaymentReceipt.objects.exists()
    for endpoint, object_id in (
        ("billing-subscriptions", subscription.pk),
        ("billing-periods", period["id"]),
        ("billing-invoices", period["invoices"][0]["id"]),
    ):
        assert bob.get(f"/api/v1/{endpoint}/{object_id}/").status_code == 404
        assert bob.get(f"/api/v1/{endpoint}/").json()["count"] == 0
        assert staff.get(f"/api/v1/{endpoint}/{object_id}/").status_code == 200
    assert (
        alice.patch(
            f"/api/v1/billing-periods/{period['id']}/", {"paid_at": "2026-11-01"}, format="json"
        ).status_code
        == 405
    )


def test_only_staff_can_attest_exact_payment_and_owner_receipt_hides_evidence(identities, plan):
    _, alice = identities["alice"]
    _, bob = identities["bob"]
    operator, staff = identities["staff"]
    period = start(alice, plan)
    invoice_id = period["invoices"][0]["id"]
    assert pay(alice, invoice_id).status_code == 403
    assert pay(bob, invoice_id).status_code == 403
    for invalid in ({"amount_minor": 999}, {"currency": "EUR"}):
        response = pay(staff, invoice_id, **invalid)
        assert response.status_code in {400, 409}
    assert not PaymentReceipt.objects.exists()
    assert Invoice.objects.get(pk=invoice_id).status == "open"

    response = pay(staff, invoice_id)
    assert response.status_code == 200, response.data
    receipt_id = response.json()["id"]
    assert response.json()["recorded_by"] == operator.pk
    assert response.json()["provider"] == "manual-bank"
    assert Invoice.objects.get(pk=invoice_id).status == "paid"
    assert BillingPeriod.objects.get(pk=period["id"]).paid_at is not None
    owner_receipt = alice.get(f"/api/v1/billing-payments/{receipt_id}/").json()
    assert set(owner_receipt) == {
        "id",
        "invoice",
        "mode",
        "amount_minor",
        "currency",
        "recorded_at",
    }
    assert bob.get(f"/api/v1/billing-payments/{receipt_id}/").status_code == 404
    assert bob.get("/api/v1/billing-payments/").json()["count"] == 0
    assert staff.delete(f"/api/v1/billing-payments/{receipt_id}/").status_code == 405


def test_payment_replay_is_idempotent_and_cannot_be_reused_for_another_invoice(identities, plan):
    _, alice = identities["alice"]
    _, bob = identities["bob"]
    _, staff = identities["staff"]
    first = start(alice, plan)
    second = start(bob, plan)
    body = evidence()
    first_url = f"/api/v1/billing-invoices/{first['invoices'][0]['id']}/record-payment/"
    second_url = f"/api/v1/billing-invoices/{second['invoices'][0]['id']}/record-payment/"
    response = staff.post(first_url, body, format="json")
    assert response.status_code == 200, response.data
    duplicate = staff.post(first_url, body, format="json")
    assert duplicate.status_code == 200 and duplicate.json()["id"] == response.json()["id"]
    conflict = staff.post(second_url, body, format="json")
    assert conflict.status_code == 409
    assert PaymentReceipt.objects.count() == 1
    assert Invoice.objects.get(pk=second["invoices"][0]["id"]).status == "open"


def test_test_and_live_accounts_are_isolated_even_for_staff_mutations(identities, plan, settings):
    _, alice = identities["alice"]
    _, staff = identities["staff"]
    test_period = start(alice, plan)
    settings.BILLING_MODE = "live"
    live_period = start(alice, plan)
    assert alice.get("/api/v1/billing-subscriptions/").json()["count"] == 1
    assert staff.get("/api/v1/billing-subscriptions/").json()["count"] == 2
    assert alice.get(f"/api/v1/billing-periods/{test_period['id']}/").status_code == 404
    assert pay(staff, test_period["invoices"][0]["id"]).status_code == 404
    settings.BILLING_MODE = "test"
    assert pay(staff, live_period["invoices"][0]["id"]).status_code == 404
    assert not PaymentReceipt.objects.exists()


def test_disabled_billing_blocks_new_admission_setup_but_allows_financial_cleanup(
    identities, plan, settings
):
    _, alice = identities["alice"]
    _, bob = identities["bob"]
    _, staff = identities["staff"]
    period = start(alice, plan, cap=100)
    settings.BILLING_ENABLED = False
    response = bob.post("/api/v1/billing-subscriptions/", {"plan_id": str(plan.pk)}, format="json")
    assert response.status_code == 503 and response.json()["code"] == "billing_disabled"
    sub_url = f"/api/v1/billing-subscriptions/{period['subscription']}/"
    assert alice.post(sub_url + "renew/", {}, format="json").status_code == 503
    assert pay(staff, period["invoices"][0]["id"]).status_code == 200
    response = alice.post(
        sub_url + "set-overage-limit/", {"overage_limit_minor": 500}, format="json"
    )
    assert response.status_code == 200, response.data
    assert response.json()["next_overage_limit_minor"] == 500
    assert BillingPeriod.objects.get(pk=period["id"]).overage_limit_minor == 100
    response = alice.post(sub_url + "cancel/", {}, format="json")
    assert response.status_code == 200 and response.json()["cancel_at_period_end"]


def test_owner_can_close_settled_ended_period_and_reads_capped_overage_invoice(
    identities, plan, monkeypatch, settings
):
    _, alice = identities["alice"]
    _, bob = identities["bob"]
    _, staff = identities["staff"]
    period = start(alice, plan, cap=2)
    assert pay(staff, period["invoices"][0]["id"]).status_code == 200
    obj = BillingPeriod.objects.get(pk=period["id"])
    BillingPeriod.objects.filter(pk=obj.pk).update(used_tokens=1200, used_calls=11)
    url = f"/api/v1/billing-periods/{obj.pk}/close/"
    assert alice.post(url, {}, format="json").status_code == 409
    assert bob.post(url, {}, format="json").status_code == 404
    monkeypatch.setattr(services.timezone, "now", lambda: obj.ends_at + timedelta(seconds=1))
    settings.BILLING_ENABLED = False
    response = alice.post(url, {}, format="json")
    assert response.status_code == 200, response.data
    assert response.json()["kind"] == "overage"
    assert response.json()["amount_minor"] == 2
    assert str(response.json()["raw_overage_minor"]) == "3"
    assert response.json()["status"] == "open"
    again = alice.post(url, {}, format="json")
    assert again.status_code == 200 and again.json()["id"] == response.json()["id"]
    assert pay(staff, response.json()["id"], amount_minor=2).status_code == 200


def test_actions_reject_paid_flags_and_staff_cannot_change_another_owners_consent(identities, plan):
    _, alice = identities["alice"]
    _, staff = identities["staff"]
    period = start(alice, plan)
    sub_url = f"/api/v1/billing-subscriptions/{period['subscription']}/"
    assert staff.post(sub_url + "cancel/", {}, format="json").status_code == 404
    for action, body in (
        ("renew", {"paid": True}),
        ("cancel", {"status": "active"}),
        ("set-overage-limit", {"overage_limit_minor": 100, "owner": 999}),
    ):
        assert alice.post(sub_url + action + "/", body, format="json").status_code == 400
    payment = evidence(mode="live", paid_at="2026-11-01T00:00:00Z")
    response = staff.post(
        f"/api/v1/billing-invoices/{period['invoices'][0]['id']}/record-payment/",
        payment,
        format="json",
    )
    assert response.status_code == 400 and not PaymentReceipt.objects.exists()
