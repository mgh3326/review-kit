"""Small standard-library client for the Miro REST API (v2)."""
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request  # nosec B404 -- HTTPS requests to the configured API host


DEFAULT_BASE_URL = "https://api.miro.com"
DEFAULT_RETRIES = 4
MAX_RETRY_DELAY = 60.0
ITEM_PAGE = "/v2/boards/%s/items"
ITEM_URL = "/v2/boards/%s/items/%s"
COMMENTS_PAGE = "/v2/boards/%s/comments"
ITEM_COMMENTS_PAGE = "/v2/boards/%s/items/%s/comments"


class MiroError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _retry_delay(headers, attempt):
    """Retry-After seconds when present, else a small exponential backoff."""
    raw = headers.get("Retry-After") if headers else None
    try:
        delay = float(raw) if raw is not None else min(2 ** attempt, 8)
    except (TypeError, ValueError):
        delay = min(2 ** attempt, 8)
    return max(0.0, min(delay, MAX_RETRY_DELAY))


def encode_multipart(fields, file_field, filename, blob, content_type):
    """(body, content_type) for a multipart/form-data upload."""
    boundary = "----review-kit-" + secrets.token_hex(8)
    parts = []
    for name, value in fields:
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, value)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                  "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                  % (boundary, file_field, filename, content_type)).encode("utf-8"))
    parts.append(blob)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("ascii"))
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


class MiroClient:
    """Minimal Miro v2 wrapper. The token is never included in errors or output."""

    def __init__(self, config):
        token = (config.get("MIRO_ACCESS_TOKEN") or "").strip()
        if not token:
            raise ValueError("Profile needs MIRO_ACCESS_TOKEN, "
                             "or MIRO_ENV_FILE pointing at a file that sets it")
        self._token = token
        self.base = (config.get("MIRO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if not self.base.startswith(("https://", "http://")):
            raise ValueError("MIRO_BASE_URL must be an absolute HTTP(S) URL")
        try:
            self.retries = max(0, int(config.get("MIRO_MAX_RETRIES", DEFAULT_RETRIES)))
        except (TypeError, ValueError):
            raise ValueError("MIRO_MAX_RETRIES must be a non-negative integer")
        self.sleep = time.sleep

    def _request(self, method, path, params=None, payload=None, multipart=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": "Bearer " + self._token}
        body = None
        if multipart is not None:
            body, headers["Content-Type"] = multipart
        elif payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        raw = b""
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=body, method=method.upper(),
                                             headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
                    raw = response.read()
                    break
            except urllib.error.HTTPError as exc:
                if (exc.code == 429 or exc.code >= 500) and attempt < self.retries:
                    exc.read(1024)
                    self.sleep(_retry_delay(exc.headers, attempt))
                    continue
                exc.read(2048)
                raise MiroError("Miro request failed (HTTP %d)" % exc.code, exc.code)
            except OSError as exc:
                raise MiroError("Miro request failed (%s)" % type(exc).__name__)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise MiroError("Miro returned an invalid JSON response")

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, payload=None, multipart=None):
        return self._request("POST", path, payload=payload, multipart=multipart)

    def patch(self, path, payload):
        return self._request("PATCH", path, payload=payload)

    def delete(self, path):
        return self._request("DELETE", path)

    def list_all(self, path, params=None):
        """Follow cursor pagination and return the full item list."""
        params = dict(params or {})
        params.setdefault("limit", 50)
        found = []
        while True:
            page = self.get(path, params)
            items = page.get("data")
            if not isinstance(items, list):
                raise MiroError("Miro returned an invalid listing")
            found.extend(items)
            cursor = page.get("cursor")
            if not cursor:
                return found
            params["cursor"] = cursor

    def items(self, board_id):
        return self.list_all(ITEM_PAGE % board_id)

    def create(self, board_id, kind, payload=None, multipart=None):
        return self.post("/v2/boards/%s/%s" % (board_id, kind),
                         payload=payload, multipart=multipart)

    def update_item(self, board_id, item_id, payload):
        return self.patch(ITEM_URL % (board_id, item_id), payload)

    def delete_item(self, board_id, item_id):
        return self.delete(ITEM_URL % (board_id, item_id))

    def comments(self, board_id):
        """Board-level comments, falling back to per-item comments on older APIs."""
        try:
            return self.list_all(COMMENTS_PAGE % board_id)
        except MiroError as exc:
            if exc.status != 404:
                raise
        found = []
        for item in self.items(board_id):
            found.extend(self.list_all(ITEM_COMMENTS_PAGE % (board_id, item["id"])))
        return found
