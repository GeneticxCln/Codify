"""CORS: the browser preview's origin is decided by Vite, not by this repo.

The dev server binds the IPv6 loopback (`[::1]:5173`), and a browser that lands
on `http://[::1]:5173` sends that literal as its `Origin` on every fetch the UI
makes to `http://127.0.0.1:7430`. The engine's allow-list did not contain it, so
every request died at the preflight — the app showed "Offline" with a working
engine behind it, which is the exact failure a CORS allow-list produces when the
origin it is shown is not one it named.

Why the browser can end up there at all: Vite prints and serves
`http://localhost:5173/`, but the socket is bound on `[::1]`, so an address-bar
entry, a bookmark, a history entry or a saved tab can hold the IPv6 literal.
`localhost` and `[::1]` are *different origins* to a browser — separate
localStorage, separate `Origin` headers — so "the other URL works" is not an
argument for leaving this one out.

These tests drive the real middleware stack (the ASGI app, `CORSMiddleware`
outside the auth middleware, the way `engine/app.py` registers them), because
the thing being pinned is not `UI_ORIGINS` the list — it is what a browser
actually receives. A unit assertion on the list would pass while the
middleware sat mis-registered and every preflight still died.

The negative cases are the ones that keep this an allow-list: the engine is
reachable from any page on the open internet's behalf if it answered any
origin, so a foreign origin must get no CORS grant — and an allow-listed
origin must still be refused a *response* it is not entitled to (a 401 stays a
401; CORS grants reading the response, never the response itself).
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app

# The origin Vite actually serves the browser preview from when the tab is on
# the IPv6 loopback. This is the one that was missing.
PREVIEW_V6 = "http://[::1]:5173"

UI_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    PREVIEW_V6,
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
]


class CORSPreflight(unittest.IsolatedAsyncioTestCase):
    """A preflight is the first request a cross-origin fetch makes.

    If it fails, nothing the UI does afterwards matters — the health check, the
    model catalog and the goal stream all die before they start. So the preflight
    is the primary subject here, per origin, not a detail.
    """

    async def asyncSetUp(self) -> None:
        self.transport = ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def _preflight(self, origin: str) -> httpx.Response:
        return await self.client.options(
            "/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    async def test_every_allow_listed_ui_origin_passes_preflight(self) -> None:
        for origin in UI_ORIGINS:
            with self.subTest(origin=origin):
                response = await self._preflight(origin)
                self.assertEqual(response.status_code, 200, f"preflight died for {origin}")
                self.assertEqual(
                    response.headers.get("access-control-allow-origin"),
                    origin,
                    f"preflight for {origin} did not echo the origin back",
                )
                # The UI sends the bearer token on every request, so the
                # preflight has to clear the Authorization header too — a
                # preflight that answers but refuses the header is a preflight
                # that failed in a different coat.
                self.assertIn(
                    "authorization",
                    (response.headers.get("access-control-allow-headers") or "").lower(),
                    f"preflight for {origin} did not clear the Authorization header",
                )

    async def test_the_ipv6_preview_origin_is_allowed_by_name(self) -> None:
        # Pinned alone, not folded into the loop above, so when this fails the
        # failure is the one sentence that matters: the preview's own origin is
        # not on the list.
        response = await self._preflight(PREVIEW_V6)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), PREVIEW_V6)

    async def test_a_foreign_origin_gets_no_cors_grant(self) -> None:
        # The engine binds 127.0.0.1, but a user's browser is an open proxy for
        # it: any web page can *send* the request. What CORS decides is whether
        # that page may *read* the answer. `evil.example` must not be granted.
        response = await self._preflight("http://evil.example")
        self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_an_options_request_that_is_not_a_preflight_still_needs_the_token(self) -> None:
        # A preflight is answered by `CORSMiddleware` before the auth middleware sees it (the tests above). What
        # is left is an OPTIONS a browser did not send as a preflight: no `Origin`, or no
        # `Access-Control-Request-Method`. It used to pass the auth middleware untouched and reach the router,
        # whose 405 for a real route against a 404 for an unknown one told a client with no token which routes
        # exist. It is a 401 now, like every other request without the token.
        for headers in ({}, {"Origin": "http://localhost:5173"}):
            for path in ("/goals/anything", "/no/such/route"):
                with self.subTest(headers=headers, path=path):
                    response = await self.client.options(path, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)

    async def test_a_loopback_host_on_another_port_is_granted_by_the_regex(self) -> None:
        # `allow_origin_regex` deliberately admits any http(s) loopback host on
        # any port: dev servers drift off 5173 when it is taken, and a second
        # checkout's Vite is still the user's own machine. What the regex must
        # never do is leave the loopback — that is the boundary, and the
        # foreign-origin test is where it is held.
        response = await self._preflight("http://localhost:5174")
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5174")

    async def test_the_regex_never_leaves_the_loopback(self) -> None:
        # The boundary case stated positively: not a port, a *host*. A LAN or
        # public host is exactly the shape "any port" must not extend to.
        response = await self._preflight("http://localhost.evil.example:5173")
        self.assertNotIn("access-control-allow-origin", response.headers)


class CORSActualRequests(unittest.IsolatedAsyncioTestCase):
    """Preflight is the gate; these pin what an actual request gets."""

    async def asyncSetUp(self) -> None:
        self.transport = ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        app.state.token = BOOT_TOKEN
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_health_from_the_ipv6_preview_carries_the_origin_header(self) -> None:
        response = await self.client.get(
            "/health", headers={"Origin": PREVIEW_V6, **self.headers}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), PREVIEW_V6)
        self.assertEqual(response.json(), {"ok": True, "authenticated": True})

    async def test_health_from_a_foreign_origin_still_has_no_grant(self) -> None:
        # The request itself is answered — CORS never blocks the server from
        # responding — but the browser will refuse to hand the response to the
        # page, because the grant is absent. The absence is the security.
        response = await self.client.get(
            "/health", headers={"Origin": "http://evil.example", **self.headers}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_a_401_is_still_a_401_from_an_allowed_origin(self) -> None:
        # CORS grants reading responses; it does not mint them. The allow-list
        # must not widen what an unauthenticated caller gets.
        response = await self.client.get("/health", headers={"Origin": PREVIEW_V6})
        self.assertEqual(response.status_code, 401)
        # The middleware is registered outside auth on purpose, so even a 401
        # carries the grant — the browser can read *why* it failed, which is
        # what makes the stale-token state debuggable instead of a silent
        # network error.
        self.assertEqual(response.headers.get("access-control-allow-origin"), PREVIEW_V6)


if __name__ == "__main__":
    unittest.main()
