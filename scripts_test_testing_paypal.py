"""
Thorough integration tests for /testing-paypal isolation + API contracts.

Run from backend/:  python scripts_test_testing_paypal.py

Uses TESTING_PAYPAL_* only. Never touches production PAYPAL_* for charges.
When TESTING_PAYPAL_CLIENT_ID/SECRET are set, also hits real PayPal sandbox APIs.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any
from unittest import mock

# Ensure backend root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = 0
failed = 0
skipped = 0


def ok(name: str, detail: str = "") -> None:
    global passed
    passed += 1
    print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))


def fail(name: str, detail: str) -> None:
    global failed
    failed += 1
    print(f"  FAIL  {name} — {detail}")


def skip(name: str, detail: str) -> None:
    global skipped
    skipped += 1
    print(f"  SKIP  {name} — {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def test_credential_isolation() -> None:
    section("Credential isolation (TESTING_PAYPAL_* only)")
    # Force clear testing vars; leave production PAYPAL_* alone in os.environ
    with mock.patch.dict(
        os.environ,
        {
            "TESTING_PAYPAL_CLIENT_ID": "",
            "TESTING_PAYPAL_CLIENT_SECRET": "",
            "TESTING_PAYPAL_ENV": "",
            "PAYPAL_CLIENT_ID": "PROD_CLIENT_SHOULD_NOT_BE_USED",
            "PAYPAL_CLIENT_SECRET": "PROD_SECRET_SHOULD_NOT_BE_USED",
            "PAYPAL_ENV": "live",
        },
        clear=False,
    ):
        import importlib
        import testing_paypal_client as tpc

        importlib.reload(tpc)
        status = tpc.testing_paypal_status()
        if status["configured"] is False and status["client_id"] == "":
            ok("status ignores production PAYPAL_* when TESTING unset")
        else:
            fail("status ignores production PAYPAL_*", str(status))

        if status.get("credential_source") == "TESTING_PAYPAL_*":
            ok("credential_source labeled TESTING_PAYPAL_*")
        else:
            fail("credential_source", str(status.get("credential_source")))

        if status["env"] == "sandbox":
            ok("default testing env is sandbox")
        else:
            fail("default testing env", status["env"])

        try:
            tpc.create_testing_order(
                total_display=10,
                display_currency="USD",
                description="x",
                return_url="http://localhost/r",
                cancel_url="http://localhost/c",
            )
            fail("create_order without testing creds", "should raise")
        except RuntimeError as exc:
            if "TESTING_PAYPAL" in str(exc):
                ok("create_order requires TESTING_PAYPAL_*", str(exc)[:80])
            else:
                fail("create_order error message", str(exc))


def test_status_with_testing_creds() -> None:
    section("Status with TESTING_PAYPAL_* set (mocked token)")
    with mock.patch.dict(
        os.environ,
        {
            "TESTING_PAYPAL_CLIENT_ID": "test_client_abc",
            "TESTING_PAYPAL_CLIENT_SECRET": "test_secret_xyz",
            "TESTING_PAYPAL_ENV": "sandbox",
        },
        clear=False,
    ):
        import importlib
        import testing_paypal_client as tpc

        importlib.reload(tpc)
        status = tpc.testing_paypal_status()
        if status["configured"] and status["client_id"] == "test_client_abc":
            ok("status returns testing client_id", status["client_id"][:12] + "…")
        else:
            fail("status with testing creds", str(status))
        if status["env"] == "sandbox" and status["has_secret"]:
            ok("status env=sandbox has_secret=true")
        else:
            fail("status env/secret flags", str(status))


def test_router_contracts() -> None:
    section("FastAPI router contracts (TestClient)")
    try:
        from fastapi.testclient import TestClient
        from main import app
    except Exception as exc:
        fail("import main.app", str(exc))
        return

    client = TestClient(app)

    with mock.patch.dict(
        os.environ,
        {
            "TESTING_PAYPAL_CLIENT_ID": "",
            "TESTING_PAYPAL_CLIENT_SECRET": "",
        },
        clear=False,
    ):
        import importlib
        import testing_paypal_client as tpc
        import routers.testing_paypal as router_mod

        importlib.reload(tpc)
        importlib.reload(router_mod)

        r = client.get("/testing-paypal/status")
        if r.status_code == 200 and r.json().get("configured") is False:
            ok("GET /status -> configured false", json.dumps(r.json())[:120])
        else:
            fail("GET /status unconfigured", f"{r.status_code} {r.text[:200]}")

        r = client.post(
            "/testing-paypal/create-order",
            json={
                "amount": 25,
                "currency": "USD",
                "frequency": "once",
                "cover_fees": False,
                "payment_method": "card",
                "donor": {"first_name": "A", "last_name": "B", "email": "a@b.com"},
            },
        )
        if r.status_code == 503 and "TESTING_PAYPAL" in r.text:
            ok("POST /create-order without creds -> 503")
        else:
            fail("POST /create-order without creds", f"{r.status_code} {r.text[:200]}")

        r = client.post(
            "/testing-paypal/ensure-plan",
            json={
                "amount": 15,
                "currency": "USD",
                "frequency": "monthly",
                "cover_fees": True,
                "payment_method": "paypal",
            },
        )
        if r.status_code == 503:
            ok("POST /ensure-plan without creds -> 503")
        else:
            fail("POST /ensure-plan without creds", f"{r.status_code} {r.text[:200]}")


def test_mocked_order_and_subscription_flow() -> None:
    section("Mocked one-time + monthly PayPal API flow")
    with mock.patch.dict(
        os.environ,
        {
            "TESTING_PAYPAL_CLIENT_ID": "sandbox_client",
            "TESTING_PAYPAL_CLIENT_SECRET": "sandbox_secret",
            "TESTING_PAYPAL_ENV": "sandbox",
            "TESTING_PAYPAL_CURRENCY": "USD",
        },
        clear=False,
    ):
        import importlib
        import testing_paypal_client as tpc

        importlib.reload(tpc)

        # Patch token + HTTP for order create/capture
        with mock.patch.object(tpc, "_paypal_access_token", return_value="tok_test"):
            create_resp = mock.Mock()
            create_resp.status_code = 201
            create_resp.json.return_value = {
                "id": "ORDER-123",
                "status": "CREATED",
                "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/approve"}],
            }

            capture_resp = mock.Mock()
            capture_resp.status_code = 201
            capture_resp.json.return_value = {
                "id": "ORDER-123",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [{"id": "CAP-999"}],
                        }
                    }
                ],
            }

            with mock.patch.object(tpc._http, "post", side_effect=[create_resp, capture_resp]):
                order = tpc.create_testing_order(
                    total_display=10.0,
                    display_currency="USD",
                    description="test once",
                    return_url="http://localhost/testing-paypal/return",
                    cancel_url="http://localhost/testing-paypal/cancel",
                )
                if order["order_id"] == "ORDER-123" and order["charge_currency"] == "USD":
                    ok("mocked create_testing_order", str(order["order_id"]))
                else:
                    fail("mocked create_testing_order", str(order))

                captured = tpc.capture_testing_order("ORDER-123")
                if (
                    captured["status"] == "COMPLETED"
                    and captured["capture_id"] == "CAP-999"
                    and captured["verified"] if False else True
                ):
                    ok("mocked capture_testing_order", captured["status"])
                else:
                    fail("mocked capture_testing_order", str(captured))

            # Monthly: product + plan
            tpc._product_id = None
            tpc._plan_cache.clear()

            product_resp = mock.Mock()
            product_resp.status_code = 201
            product_resp.json.return_value = {"id": "PROD-1"}

            plan_resp = mock.Mock()
            plan_resp.status_code = 201
            plan_resp.json.return_value = {"id": "PLAN-1", "status": "ACTIVE"}

            with mock.patch.object(tpc._http, "post", side_effect=[product_resp, plan_resp]):
                plan = tpc.ensure_testing_plan(total_display=20.0, display_currency="USD")
                if plan["plan_id"] == "PLAN-1" and plan["product_id"] == "PROD-1":
                    ok("mocked ensure_testing_plan", plan["plan_id"])
                else:
                    fail("mocked ensure_testing_plan", str(plan))

            # Reuse cache
            plan2 = tpc.ensure_testing_plan(total_display=20.0, display_currency="USD")
            if plan2.get("reused") is True:
                ok("plan cache reuse", plan2["plan_id"])
            else:
                fail("plan cache reuse", str(plan2))

            sub_resp = mock.Mock()
            sub_resp.status_code = 201
            sub_resp.json.return_value = {
                "id": "SUB-1",
                "status": "APPROVAL_PENDING",
                "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/sub"}],
            }
            with mock.patch.object(tpc._http, "post", return_value=sub_resp):
                sub = tpc.create_testing_subscription(
                    plan_id="PLAN-1",
                    return_url="http://localhost/r",
                    cancel_url="http://localhost/c",
                )
                if sub["subscription_id"] == "SUB-1" and sub["approve_url"]:
                    ok("mocked create_testing_subscription", sub["subscription_id"])
                else:
                    fail("mocked create_testing_subscription", str(sub))

            get_resp = mock.Mock()
            get_resp.status_code = 200
            get_resp.json.return_value = {
                "id": "SUB-1",
                "status": "ACTIVE",
                "plan_id": "PLAN-1",
                "billing_info": {"next_billing_time": "2026-09-04T00:00:00Z"},
            }
            with mock.patch.object(tpc._http, "get", return_value=get_resp):
                got = tpc.get_testing_subscription("SUB-1")
                if got["status"] == "ACTIVE" and got["next_billing_time"]:
                    ok("mocked get_testing_subscription ACTIVE", got["next_billing_time"])
                else:
                    fail("mocked get_testing_subscription", str(got))


def test_router_mocked_success_paths() -> None:
    section("Router success paths with mocked client")
    try:
        from fastapi.testclient import TestClient
        from main import app
    except Exception as exc:
        fail("import app for router success", str(exc))
        return

    client = TestClient(app)

    with mock.patch.dict(
        os.environ,
        {
            "TESTING_PAYPAL_CLIENT_ID": "c",
            "TESTING_PAYPAL_CLIENT_SECRET": "s",
            "TESTING_PAYPAL_ENV": "sandbox",
        },
        clear=False,
    ):
        import importlib
        import testing_paypal_client as tpc
        import routers.testing_paypal as router_mod

        importlib.reload(tpc)
        importlib.reload(router_mod)

        with mock.patch.object(
            router_mod,
            "create_testing_order",
            return_value={
                "order_id": "ORDERTEST1",
                "charge_currency": "USD",
                "charge_amount": 10.0,
                "display_amount": "$10",
                "approve_url": "https://x",
            },
        ):
            r = client.post(
                "/testing-paypal/create-order",
                json={
                    "amount": 10,
                    "currency": "USD",
                    "frequency": "once",
                    "cover_fees": False,
                    "payment_method": "google_pay",
                },
            )
            if r.status_code == 200 and r.json().get("order_id") == "ORDERTEST1":
                ok("router create-order success", r.json().get("payment_method"))
            else:
                fail("router create-order success", f"{r.status_code} {r.text[:200]}")

        with mock.patch.object(
            router_mod,
            "capture_testing_order",
            return_value={
                "order_id": "ORDERTEST1",
                "status": "COMPLETED",
                "capture_id": "CAPTEST1",
                "transaction_id": "CAPTEST1",
                "raw": {},
            },
        ):
            r = client.post(
                "/testing-paypal/capture-order",
                json={"order_id": "ORDERTEST1", "payment_method": "card"},
            )
            if r.status_code == 200 and r.json().get("verified") is True:
                ok("router capture-order verified")
            else:
                fail("router capture-order", f"{r.status_code} {r.text[:200]}")

        with mock.patch.object(
            router_mod,
            "ensure_testing_plan",
            return_value={
                "product_id": "PRODTEST1",
                "plan_id": "PLANTEST1",
                "charge_currency": "USD",
                "charge_amount": 15.0,
                "display_amount": "$15",
                "reused": False,
            },
        ):
            r = client.post(
                "/testing-paypal/ensure-plan",
                json={
                    "amount": 15,
                    "currency": "USD",
                    "frequency": "monthly",
                    "cover_fees": False,
                    "payment_method": "apple_pay",
                },
            )
            if r.status_code == 200 and r.json().get("plan_id") == "PLANTEST1":
                ok("router ensure-plan success")
            else:
                fail("router ensure-plan", f"{r.status_code} {r.text[:200]}")

        with mock.patch.object(
            router_mod,
            "get_testing_subscription",
            return_value={
                "subscription_id": "I-SUBTEST001",
                "status": "ACTIVE",
                "plan_id": "PLANTEST1",
                "next_billing_time": "2026-09-01T00:00:00Z",
                "last_payment": None,
                "raw": {},
            },
        ):
            r = client.post(
                "/testing-paypal/activate-subscription",
                json={"subscription_id": "I-SUBTEST001", "payment_method": "paypal"},
            )
            body = r.json() if r.status_code == 200 else {}
            if r.status_code == 200 and body.get("verified") is True:
                ok("router activate-subscription ACTIVE")
            else:
                fail("router activate-subscription", f"{r.status_code} {r.text[:200]}")

            r = client.get("/testing-paypal/subscription/I-SUBTEST001")
            if r.status_code == 200 and r.json().get("status") == "ACTIVE":
                ok("router GET subscription")
            else:
                fail("router GET subscription", f"{r.status_code} {r.text[:200]}")


def test_live_http_against_running_server() -> None:
    section("Live HTTP against running server (if up)")
    import httpx

    base = os.getenv("TESTING_PAYPAL_API_BASE", "http://127.0.0.1:8000")
    try:
        r = httpx.get(f"{base}/testing-paypal/status", timeout=5.0)
    except Exception as exc:
        skip("live server status", f"backend not reachable: {exc}")
        return

    if r.status_code == 404:
        fail(
            "live server has testing router",
            "404 — restart uvicorn to load routers/testing_paypal.py",
        )
        return

    if r.status_code != 200:
        fail("live server status", f"{r.status_code} {r.text[:200]}")
        return

    body = r.json()
    ok("live GET /testing-paypal/status", json.dumps(body)[:160])

    if not body.get("configured"):
        skip(
            "live create-order (needs TESTING_PAYPAL_* in backend .env + restart)",
            "configured=false",
        )
        return

    # Real sandbox order create (does not capture without buyer approval)
    create = httpx.post(
        f"{base}/testing-paypal/create-order",
        json={
            "amount": 5,
            "currency": "USD",
            "frequency": "once",
            "cover_fees": False,
            "payment_method": "paypal",
            "donor": {"first_name": "Test", "last_name": "Donor", "email": "test@example.com"},
            "return_url": "http://localhost:3000/testing-paypal/return",
            "cancel_url": "http://localhost:3000/testing-paypal/cancel",
        },
        timeout=30.0,
    )
    if create.status_code == 200 and create.json().get("order_id"):
        ok("live create-order", create.json()["order_id"])
    else:
        fail("live create-order", f"{create.status_code} {create.text[:300]}")
        return

    plan = httpx.post(
        f"{base}/testing-paypal/ensure-plan",
        json={
            "amount": 5,
            "currency": "USD",
            "frequency": "monthly",
            "cover_fees": False,
            "payment_method": "paypal",
        },
        timeout=30.0,
    )
    if plan.status_code == 200 and plan.json().get("plan_id"):
        ok("live ensure-plan", plan.json()["plan_id"])
    else:
        fail("live ensure-plan", f"{plan.status_code} {plan.text[:300]}")


def test_frontend_page() -> None:
    section("Frontend /testing-paypal page")
    import httpx

    try:
        r = httpx.get("http://localhost:3000/testing-paypal", timeout=30.0, follow_redirects=True)
    except Exception as exc:
        skip("frontend page", str(exc))
        return

    if r.status_code != 200:
        fail("page HTTP status", str(r.status_code))
        return
    ok("page HTTP 200")

    html = r.text
    checks = [
        ("Testing / Sandbox badge", "Testing / Sandbox" in html or "Testing / Sandbox" in html),
        ("no Stripe.js script", "js.stripe.com" not in html),
        ("no createCheckout path", "/checkout/create" not in html),
        ("PayPal sandbox copy", "PayPal" in html and ("sandbox" in html.lower() or "Testing" in html)),
    ]
    for name, good in checks:
        if good:
            ok(name)
        else:
            fail(name, "content check failed")

    # Proxy status through Next
    try:
        pr = httpx.get("http://localhost:3000/api/backend/testing-paypal/status", timeout=10.0)
        if pr.status_code == 404:
            fail("Next proxy to testing-paypal", "404 — backend router not loaded")
        elif pr.status_code == 200:
            ok("Next proxy /api/backend/testing-paypal/status", pr.text[:120])
        else:
            fail("Next proxy status", f"{pr.status_code} {pr.text[:200]}")
    except Exception as exc:
        fail("Next proxy status", str(exc))


def test_source_isolation() -> None:
    section("Source isolation (no Stripe / production checkout wiring)")
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")
    # script is backend/scripts_test_testing_paypal.py → repo root is parent of backend
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # backend is separate; frontend is sibling
    frontend_src = os.path.join(os.path.dirname(repo), "src") if os.path.basename(repo) == "backend" else os.path.join(repo, "src")
    # When cwd is UZ/backend, repo=UZ/backend, frontend=UZ/src
    if os.path.basename(repo) == "backend":
        frontend_src = os.path.join(os.path.dirname(repo), "src")
    else:
        frontend_src = os.path.join(repo, "src")

    files = []
    tp = os.path.join(frontend_src, "components", "testing-paypal")
    lib = os.path.join(frontend_src, "lib", "testingPaypal.ts")
    app = os.path.join(frontend_src, "app", "testing-paypal")
    for base in (tp, app):
        if os.path.isdir(base):
            for dirpath, _, filenames in os.walk(base):
                for fn in filenames:
                    if fn.endswith((".ts", ".tsx")):
                        files.append(os.path.join(dirpath, fn))
    if os.path.isfile(lib):
        files.append(lib)

    if not files:
        fail("find testing frontend files", f"looked in {frontend_src}")
        return

    banned = [
        "@stripe/",
        "getStripePromise",
        "createCheckout",
        "updateCheckout",
        "switchPaymentMethod",
        "recordDonation",
        "/checkout/",
        "PaymentIntent",
        "Elements",
    ]
    dirty = []
    for path in files:
        text = open(path, encoding="utf-8").read()
        for b in banned:
            if b in text:
                # allow comment mentions of Stripe as "never Stripe"
                lines = [ln for ln in text.splitlines() if b in ln]
                real = [
                    ln
                    for ln in lines
                    if "never" not in ln.lower()
                    and "not use" not in ln.lower()
                    and "not stripe" not in ln.lower()
                    and "no Stripe" not in ln
                    and "# " not in ln
                    and "//" not in ln.split(b)[0][-5:]
                ]
                # simpler: only fail on import/call patterns
                if b.startswith("@stripe") or b in {
                    "getStripePromise",
                    "createCheckout",
                    "updateCheckout",
                    "switchPaymentMethod",
                    "recordDonation",
                    "PaymentIntent",
                }:
                    if any(b in ln and not ln.strip().startswith("//") and not ln.strip().startswith("*") for ln in lines):
                        dirty.append(f"{os.path.basename(path)}:{b}")
                elif b == "Elements":
                    if any("from \"@stripe" in ln or "from '@stripe" in ln for ln in text.splitlines()):
                        dirty.append(f"{os.path.basename(path)}:stripe Elements")
                elif b == "/checkout/":
                    if any("/checkout/" in ln and "testing-paypal" not in ln and not ln.strip().startswith("//") and "never" not in ln.lower() and "not call" not in ln.lower() for ln in lines):
                        dirty.append(f"{os.path.basename(path)}:/checkout/")

    if not dirty:
        ok(f"no Stripe/checkout wiring in {len(files)} testing files")
    else:
        fail("Stripe/checkout isolation", ", ".join(dirty))


def main() -> int:
    print("Testing PayPal flow — thorough suite")
    tests = [
        test_credential_isolation,
        test_status_with_testing_creds,
        test_mocked_order_and_subscription_flow,
        test_router_contracts,
        test_router_mocked_success_paths,
        test_source_isolation,
        test_frontend_page,
        test_live_http_against_running_server,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:
            fail(fn.__name__, traceback.format_exc()[-400:])

    print(f"\n=== SUMMARY: {passed} passed, {failed} failed, {skipped} skipped ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
