import json

import requests


def make_response(
    status_code: int, json_body=None, raw_body: bytes | None = None
) -> requests.Response:
    """Build a real requests.Response so .raise_for_status()/.json() behave
    exactly as they would for a live call, without touching the network."""
    response = requests.Response()
    response.status_code = status_code
    if raw_body is not None:
        response._content = raw_body
    elif json_body is not None:
        response._content = json.dumps(json_body).encode("utf-8")
    else:
        response._content = b""
    return response
