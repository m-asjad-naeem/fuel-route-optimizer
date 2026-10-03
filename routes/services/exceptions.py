"""Provider-facing exception hierarchy.

Application code must never see raw `requests` exceptions or HTTP status
codes — every provider implementation catches those at its boundary and
re-raises one of these instead.
"""


class ProviderError(Exception):
    """Base class for any external-provider failure."""


class GeocodingProviderError(ProviderError):
    """The geocoding provider failed (timeout, HTTP error, malformed response)."""


class LocationNotFoundError(GeocodingProviderError):
    """The provider responded successfully but found no match for the location.

    Deliberately a distinct exception from GeocodingProviderError: "the
    provider is broken" and "this address doesn't exist" require different
    handling (502 vs. 422 at the API layer).
    """


class RoutingProviderError(ProviderError):
    """The routing provider failed (timeout, HTTP error, malformed response)."""


class RouteNotFoundError(RoutingProviderError):
    """The provider responded successfully but found no route between the points."""
