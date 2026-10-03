"""Application-layer exceptions raised by RoutePlanService.

These are distinct from the provider exceptions in exceptions.py:
provider exceptions describe "what went wrong talking to Nominatim/OSRM";
these describe "what went wrong with this specific route-plan request,
in terms the API layer can turn directly into a response." RoutePlanService
translates a provider's LocationNotFoundError into
RoutePlanLocationNotFoundError (adding which field — start or destination —
failed), and leaves genuine provider failures (timeouts, 5xx, malformed
responses) to propagate as-is, since the view maps those straight to 502.
"""
from __future__ import annotations


class RoutePlanLocationNotFoundError(Exception):
    """A location string could not be geocoded to any result."""

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(message)


class RoutePlanLocationOutOfScopeError(Exception):
    """A resolved location (geocoded or supplied as coordinates) falls
    outside the application's supported USA service area.

    Deliberately enforced here, at the application boundary — not inside
    GeocodingProvider/RoutingProvider, which stay geographically generic
    and reusable regardless of the application's USA-only scope.
    """

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(message)
