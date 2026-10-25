"""Small standard-library storage clients for review-kit."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def _supabase_object_missing(status, body):
    if status == 404:
        return True
    if status != 400:
        return False
    try:
        message = json.loads(body.decode("utf-8")).get("message", "")
    except (AttributeError, ValueError, UnicodeDecodeError):
        return False
    return str(message).strip().lower() in ("object not found", "not found")


def _quote_path(value):
    return urllib.parse.quote(value, safe="/-_.~")


def _quote_query(value):
    return urllib.parse.quote(str(value), safe="-_.~")


def canonical_query(params):
    """AWS SigV4 canonical query from key/value pairs."""
    pairs = [(_quote_query(k), _quote_query(v)) for k, v in params]
    pairs.sort()
    return "&".join("%s=%s" % pair for pair in pairs)


def signing_key(secret, date, region, service="s3"):
    def sign(key, value):
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    key = sign(("AWS4" + secret).encode("utf-8"), date)
    key = sign(key, region)
    key = sign(key, service)
    return sign(key, "aws4_request")


def sigv4_signature(method, path, query, headers, payload_hash, access_key,
                    secret_key, region, timestamp):
    """Return (authorization, signature) for an S3 SigV4 header request."""
    amzdate = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date = timestamp.strftime("%Y%m%d")
    normalized = {str(k).lower(): " ".join(str(v).strip().split())
                  for k, v in headers.items()}
    normalized["x-amz-date"] = amzdate
    names = sorted(normalized)
    signed_headers = ";".join(names)
    canonical_headers = "".join("%s:%s\n" % (name, normalized[name]) for name in names)
    canonical = "\n".join((method.upper(), path or "/", canonical_query(query),
                            canonical_headers, signed_headers, payload_hash))
    scope = "%s/%s/s3/aws4_request" % (date, region)
    string_to_sign = "\n".join(("AWS4-HMAC-SHA256", amzdate, scope,
                                 hashlib.sha256(canonical.encode("utf-8")).hexdigest()))
    signature = hmac.new(signing_key(secret_key, date, region),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = ("AWS4-HMAC-SHA256 Credential=%s/%s,SignedHeaders=%s,Signature=%s" %
                     (access_key, scope, signed_headers, signature))
    return authorization, signature


def sigv4_presigned_url(base_url, host, path, params, access_key, secret_key,
                        region, timestamp, expires, session_token=None):
    """Create an S3 GET URL using SigV4 query authentication."""
    if not 1 <= int(expires) <= 604800:
        raise ValueError("Signed URL duration must be between 1 and 604800 seconds")
    date = timestamp.strftime("%Y%m%d")
    amzdate = timestamp.strftime("%Y%m%dT%H%M%SZ")
    scope = "%s/%s/s3/aws4_request" % (date, region)
    query = [
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
        ("X-Amz-Credential", access_key + "/" + scope),
        ("X-Amz-Date", amzdate),
        ("X-Amz-Expires", str(int(expires))),
        ("X-Amz-SignedHeaders", "host"),
    ]
    if session_token:
        query.append(("X-Amz-Security-Token", session_token))
    canonical = "\n".join(("GET", path or "/", canonical_query(query),
                            "host:%s\n" % host, "host", UNSIGNED_PAYLOAD))
    string_to_sign = "\n".join(("AWS4-HMAC-SHA256", amzdate, scope,
                                 hashlib.sha256(canonical.encode("utf-8")).hexdigest()))
    signature = hmac.new(signing_key(secret_key, date, region),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return base_url + "?" + canonical_query(query) + "&X-Amz-Signature=" + signature


class SupabaseBackend:
    name = "supabase"

    def __init__(self, config, timeout=120):
        self.base_url = config["SUPABASE_URL"].rstrip("/")
        self.key = config.get("SUPABASE_SERVICE_KEY") or config.get("SUPABASE_SECRET_KEY")
        self.bucket = config["SUPABASE_BUCKET"]
        self.timeout = timeout
        if not self.key:
            raise ValueError("Supabase profile needs SUPABASE_SERVICE_KEY")

    def _url(self, path):
        return self.base_url + "/storage/v1" + path

    def _request(self, method, path, body=None, content_type="application/json", extra=None):
        headers = {"apikey": self.key, "Authorization": "Bearer " + self.key}
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if data is not None:
            headers["content-type"] = content_type
        headers.update(extra or {})
        req = urllib.request.Request(self._url(path), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            # Never surface response bodies: some storage services echo request details.
            return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read(1024)
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("Supabase request failed (%s)" % type(exc).__name__)

    def _object_path(self, key):
        return "/object/%s/%s" % (urllib.parse.quote(self.bucket, safe=""), _quote_path(key))

    @staticmethod
    def _metadata_sha(info):
        return ((info.get("metadata") or {}).get("sha256") or
                (info.get("user_metadata") or {}).get("sha256"))

    def _put(self, key, data, content_type, digest):
        info_path = "/object/info/%s/%s" % (urllib.parse.quote(self.bucket, safe=""),
                                               _quote_path(key))
        status, _, raw = self._request("GET", info_path)
        if status == 200:
            try:
                info = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                info = {}
            if self._metadata_sha(info) == digest:
                return False
            raise RuntimeError("Refusing to overwrite an object with a different or unknown SHA256")
        if not _supabase_object_missing(status, raw):
            raise RuntimeError("Supabase object check failed (HTTP %d)" % status)
        metadata = base64.b64encode(json.dumps({"sha256": digest}).encode("utf-8")).decode("ascii")
        status, _, _ = self._request("POST", self._object_path(key), data, content_type,
                                     {"x-upsert": "false", "x-metadata": metadata})
        if status not in (200, 201):
            raise RuntimeError("Supabase upload failed (HTTP %d)" % status)
        return True

    def put_file(self, key, path, content_type, digest):
        with open(path, "rb") as source:
            return self._put(key, source.read(), content_type, digest)

    def put_bytes(self, key, data, content_type, digest):
        return self._put(key, data, content_type, digest)

    def replace_bytes(self, key, data, content_type, digest):
        info_path = "/object/info/%s/%s" % (urllib.parse.quote(self.bucket, safe=""),
                                               _quote_path(key))
        status, _, raw = self._request("GET", info_path)
        missing = _supabase_object_missing(status, raw)
        if status != 200 and not missing:
            raise RuntimeError("Supabase object check failed (HTTP %d)" % status)
        metadata = base64.b64encode(json.dumps({"sha256": digest}).encode("utf-8")).decode("ascii")
        method = "POST" if missing else "PUT"
        status, _, _ = self._request(method, self._object_path(key), data, content_type,
                                     {"x-upsert": "true", "x-metadata": metadata})
        if status not in (200, 201):
            raise RuntimeError("Supabase INDEX refresh failed (HTTP %d)" % status)

    def object_sha256(self, key):
        path = "/object/info/%s/%s" % (urllib.parse.quote(self.bucket, safe=""),
                                         _quote_path(key))
        status, _, raw = self._request("GET", path)
        if _supabase_object_missing(status, raw):
            return None
        if status != 200:
            raise RuntimeError("Supabase object check failed (HTTP %d)" % status)
        try:
            return self._metadata_sha(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return None

    def sign_get(self, key, expires):
        path = "/object/sign/%s/%s" % (urllib.parse.quote(self.bucket, safe=""), _quote_path(key))
        status, _, raw = self._request("POST", path, {"expiresIn": int(expires)})
        if status != 200:
            raise RuntimeError("Supabase signed-link request failed (HTTP %d)" % status)
        try:
            signed = json.loads(raw.decode("utf-8"))["signedURL"]
        except (ValueError, KeyError, UnicodeDecodeError, TypeError):
            raise RuntimeError("Supabase returned an invalid signed-link response")
        if signed.startswith("https://") or signed.startswith("http://"):
            return signed
        return self.base_url + "/storage/v1" + (signed if signed.startswith("/") else "/" + signed)

    def _list_dir(self, prefix):
        results = []
        offset = 0
        while True:
            body = {"prefix": prefix, "limit": 1000, "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"}}
            status, _, raw = self._request("POST", "/object/list/" +
                                           urllib.parse.quote(self.bucket, safe=""), body)
            if status != 200:
                raise RuntimeError("Supabase object listing failed (HTTP %d)" % status)
            try:
                page = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError("Supabase returned an invalid object listing")
            if not isinstance(page, list):
                raise RuntimeError("Supabase returned an invalid object listing")
            results.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
        return results

    def list_objects(self, prefix=""):
        root = prefix.strip("/")
        found = []
        seen_dirs = set()

        def walk(directory):
            if directory in seen_dirs:
                return
            seen_dirs.add(directory)
            for item in self._list_dir(directory):
                name = str(item.get("name", ""))
                if not name or name in (".", ".."):
                    continue
                key = (directory + "/" + name) if directory else name
                metadata = item.get("metadata") or {}
                if item.get("id") is None and not metadata:
                    walk(key)
                else:
                    try:
                        size = int(metadata.get("size", item.get("size", 0)) or 0)
                    except (TypeError, ValueError):
                        size = 0
                    if not root or key == root or key.startswith(root + "/"):
                        found.append((key, size))

        walk(root)
        return sorted(found)

    def delete_keys(self, keys):
        for start in range(0, len(keys), 100):
            batch = keys[start:start + 100]
            path = "/object/" + urllib.parse.quote(self.bucket, safe="")
            status, _, _ = self._request("DELETE", path, {"prefixes": batch})
            if status not in (200, 204):
                raise RuntimeError("Supabase deletion failed (HTTP %d)" % status)


class S3Backend:
    """S3 API client. Supports path-style and virtual-host addressing."""
    name = "s3"

    def __init__(self, config, timeout=120):
        self.endpoint = urllib.parse.urlsplit(config["S3_ENDPOINT"])
        if self.endpoint.scheme not in ("https", "http") or not self.endpoint.netloc:
            raise ValueError("S3_ENDPOINT must be an absolute HTTP(S) URL")
        self.bucket = config["S3_BUCKET"]
        self.region = config.get("S3_REGION", "us-east-1")
        self.access_key = config["S3_ACCESS_KEY_ID"]
        self.secret_key = config["S3_SECRET_ACCESS_KEY"]
        self.session_token = config.get("S3_SESSION_TOKEN")
        self.addressing = config.get("S3_ADDRESSING_STYLE", "path").lower()
        if self.addressing not in ("path", "virtual"):
            raise ValueError("S3_ADDRESSING_STYLE must be path or virtual")
        self.timeout = timeout

    def _target(self, key):
        base_path = self.endpoint.path.rstrip("/")
        if self.addressing == "virtual":
            host = self.bucket + "." + self.endpoint.netloc
            path = base_path + "/" + _quote_path(key)
        else:
            host = self.endpoint.netloc
            path = base_path + "/" + _quote_path(self.bucket + "/" + key)
        return host, path or "/", self.endpoint.scheme + "://" + host + (path or "/")

    def _request(self, method, key="", body=b"", extra=None, query=(), payload_hash=None):
        host, path, url = self._target(key)
        body = body or b""
        digest = payload_hash or hashlib.sha256(body).hexdigest()
        timestamp = dt.datetime.now(dt.timezone.utc)
        headers = {"host": host, "x-amz-content-sha256": digest,
                   "x-amz-date": timestamp.strftime("%Y%m%dT%H%M%SZ")}
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        headers.update({str(k).lower(): str(v) for k, v in (extra or {}).items()})
        authorization, _ = sigv4_signature(method, path, query, headers, digest,
                                            self.access_key, self.secret_key,
                                            self.region, timestamp)
        send_headers = dict(headers)
        send_headers["Authorization"] = authorization
        request_url = url + ("?" + canonical_query(query) if query else "")
        request = urllib.request.Request(request_url,
                                         data=(body if method.upper() not in ("GET", "HEAD", "DELETE") else None),
                                         method=method.upper(), headers=send_headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read(65536)
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("S3 request failed (%s)" % type(exc).__name__)

    @staticmethod
    def _sha_from_headers(headers):
        lowered = {k.lower(): v for k, v in headers.items()}
        return lowered.get("x-amz-meta-sha256")

    def _put(self, key, data, content_type, digest):
        status, headers, _ = self._request("HEAD", key)
        if status == 200:
            if self._sha_from_headers(headers) == digest:
                return False
            raise RuntimeError("Refusing to overwrite an object with a different or unknown SHA256")
        if status != 404:
            raise RuntimeError("S3 object check failed (HTTP %d)" % status)
        status, _, _ = self._request("PUT", key, data,
                                     {"content-type": content_type,
                                      "x-amz-meta-sha256": digest})
        if status not in (200, 201, 204):
            raise RuntimeError("S3 upload failed (HTTP %d)" % status)
        return True

    def put_file(self, key, path, content_type, digest):
        with open(path, "rb") as source:
            return self._put(key, source.read(), content_type, digest)

    def put_bytes(self, key, data, content_type, digest):
        return self._put(key, data, content_type, digest)

    def replace_bytes(self, key, data, content_type, digest):
        status, _, _ = self._request("PUT", key, data,
                                     {"content-type": content_type,
                                      "x-amz-meta-sha256": digest})
        if status not in (200, 201, 204):
            raise RuntimeError("S3 INDEX refresh failed (HTTP %d)" % status)

    def object_sha256(self, key):
        status, headers, _ = self._request("HEAD", key)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError("S3 object check failed (HTTP %d)" % status)
        return self._sha_from_headers(headers)

    def sign_get(self, key, expires):
        host, path, url = self._target(key)
        return sigv4_presigned_url(url, host, path, [], self.access_key,
                                   self.secret_key, self.region,
                                   dt.datetime.now(dt.timezone.utc), expires,
                                   self.session_token)

    @staticmethod
    def _xml_local(element, name):
        return [child for child in element.iter()
                if child.tag.rsplit("}", 1)[-1] == name]

    def list_objects(self, prefix=""):
        found = []
        token = None
        while True:
            query = [("list-type", "2")]
            if prefix:
                query.append(("prefix", prefix))
            if token:
                query.append(("continuation-token", token))
            status, _, raw = self._request("GET", "", query=query)
            if status != 200:
                raise RuntimeError("S3 object listing failed (HTTP %d)" % status)
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                raise RuntimeError("S3 returned an invalid object listing")
            for item in self._xml_local(root, "Contents"):
                key_nodes = self._xml_local(item, "Key")
                size_nodes = self._xml_local(item, "Size")
                if key_nodes:
                    try:
                        size = int(size_nodes[0].text or "0") if size_nodes else 0
                    except ValueError:
                        size = 0
                    found.append((key_nodes[0].text or "", size))
            truncated = self._xml_local(root, "IsTruncated")
            if not truncated or (truncated[0].text or "").lower() != "true":
                break
            next_token = self._xml_local(root, "NextContinuationToken")
            if not next_token or not next_token[0].text:
                raise RuntimeError("S3 listing omitted its continuation token")
            token = next_token[0].text
        return sorted(found)

    def delete_keys(self, keys):
        for key in keys:
            status, _, _ = self._request("DELETE", key)
            if status not in (200, 202, 204, 404):
                raise RuntimeError("S3 deletion failed (HTTP %d)" % status)


def create_backend(config, backend=None):
    selected = (backend or config.get("BACKEND", "supabase")).lower()
    if selected == "supabase":
        required = ("SUPABASE_URL", "SUPABASE_BUCKET")
        missing = [name for name in required if not config.get(name)]
        if missing:
            raise ValueError("Supabase profile is missing: " + ", ".join(missing))
        return SupabaseBackend(config)
    if selected == "s3":
        required = ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
        missing = [name for name in required if not config.get(name)]
        if missing:
            raise ValueError("S3 profile is missing: " + ", ".join(missing))
        return S3Backend(config)
    raise ValueError("Backend must be supabase or s3")
