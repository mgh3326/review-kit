"""Small standard-library storage clients for review-kit."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .drive import default_run, drive_status, load_hosts, make_host


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


def readable_key(prefix, relative_path=None):
    """Traversal-free key that keeps names as people see them (Drive and Dropbox paths)."""
    raw_parts = str(prefix).replace("\\", "/").split("/")
    if relative_path:
        raw_parts += str(relative_path).replace("\\", "/").split("/")
    parts = []
    for raw in raw_parts:
        part = unicodedata.normalize("NFC", raw)
        if part in ("", ".", "..") or any(unicodedata.category(char) == "Cc" for char in part):
            raise ValueError("Paths must not contain empty, traversal or control-character "
                             "components")
        parts.append(part)
    return "/".join(parts)


def _temporary_copy(data):
    temporary = tempfile.NamedTemporaryFile(delete=False)
    try:
        temporary.write(data)
    finally:
        temporary.close()
    return temporary.name


class DriveBackend:
    """Files under a Google Drive mount, on this Mac or a mount host over ssh.

    Links are Drive paths, not URLs: reviewers open the shared Drive folder.
    The Drive copy is the canonical one, so capacity pruning never touches it.
    """
    name = "drive"
    capacity_managed = False
    object_key = staticmethod(readable_key)

    def __init__(self, host):
        if not host.drive_root:
            raise ValueError("Drive needs drive_root for %s in hosts.toml "
                             "([local] or [hosts.<name>])" % host.label)
        self.host = host
        self.root = host.drive_root

    def path(self, key):
        return self.host.join(self.root, key) if key else self.root

    def relative(self, path):
        """A key for a Drive folder given absolute (under drive_root) or relative to it;
        '' or '.' is drive_root itself."""
        path = str(path or "").replace("\\", "/").rstrip("/")
        root = str(self.root).replace("\\", "/").rstrip("/")
        if path in ("", ".") or path == root:
            return ""
        if path.startswith(root + "/"):
            path = path[len(root) + 1:]
        elif path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
            raise ValueError("Drive destination must be under drive_root (%s)" % self.root)
        return readable_key(path)

    @staticmethod
    def join_key(folder, relative):
        return readable_key(folder, relative) if folder else readable_key(relative)

    def _target(self, key):
        """The Drive path for key. A symbolic link at any level below drive_root (or the
        file itself) could point outside Drive, so it is refused before any write."""
        parts = [part for part in str(key).split("/") if part]
        chain = [self.host.join(self.root, *parts[:depth]) for depth in range(1, len(parts) + 1)]
        linked = self.host.symlinks(chain)
        if linked:
            raise DriveConflict("Refusing %s: %s is a symbolic link; Drive copies stay "
                                "under drive_root" % (self.path(key), linked[0]))
        return self.path(key)

    def already_there(self, key, digest):
        """True when key holds digest, False when it is free; DriveConflict otherwise,
        so an existing Drive file is never overwritten with other bytes."""
        target = self._target(key)
        current = self.host.sha256(target)
        if current == digest:
            return True
        if current is not None or self.host.exists(target):
            raise DriveConflict("Refusing to overwrite %s: it holds different or unreadable "
                                "bytes; choose another drive-dest or move it away" % target)
        return False

    def put_file(self, key, path, content_type, digest):
        if self.already_there(key, digest):
            return False
        self._copy(key, path, digest)
        return True

    def _copy(self, key, path, digest):
        target = self._target(key)
        self.host.put_file(path, target)
        if self.host.sha256(target) != digest:
            raise RuntimeError("Drive copy of %s does not match its sha256" % key)

    def put_bytes(self, key, data, content_type, digest):
        name = _temporary_copy(data)
        try:
            return self.put_file(key, name, content_type, digest)
        finally:
            os.unlink(name)

    def replace_bytes(self, key, data, content_type, digest):
        """resign rewrites INDEX/MANIFEST in place: no overwrite refusal, same checks."""
        name = _temporary_copy(data)
        try:
            self._copy(key, name, digest)
        finally:
            os.unlink(name)

    def object_sha256(self, key):
        return self.host.sha256(self.path(key))

    def sign_get(self, key, expires):
        return self.path(key)

    def list_objects(self, prefix=""):
        root = str(self.root).replace("\\", "/").rstrip("/") + "/"
        found = []
        for path, size in self.host.list_sizes(self.path(prefix.strip("/"))):
            path = str(path).replace("\\", "/")
            if path.startswith(root):
                found.append((path[len(root):], size))
        return sorted(found)

    def delete_keys(self, keys):
        self.host.remove([self._target(key) for key in keys])

    def upload_status(self, keys):
        """drive_status for the Drive files behind keys (File Provider upload state)."""
        return drive_status(self.host, [self.path(key) for key in keys], expand=False)


def _text(data):
    return (data or b"").decode("utf-8", "replace")


def _megabytes(count):
    return "%.1f MB" % (int(count) / 1e6)


class DriveConflict(ValueError):
    """The Drive destination already holds other bytes; no copy was made."""


class DropboxFull(RuntimeError):
    """rclone reported path/insufficient_space."""


class DropboxBackend:
    """Dropbox through an rclone remote (DROPBOX_REMOTE=remote:folder).

    rclone share links do not expire on the free plan, so INDEX expiry dates are
    the review window, not a link lifetime; share-gc and prune remove the copies.
    """
    name = "dropbox"
    object_key = staticmethod(readable_key)

    def __init__(self, config, run=None):
        remote = (config.get("DROPBOX_REMOTE") or "").strip().rstrip("/")
        if not remote or ":" not in remote or remote.startswith("-"):
            raise ValueError("Dropbox needs DROPBOX_REMOTE=<rclone remote>:<folder>")
        self.remote = remote
        self.run = run or default_run

    def _rclone(self, *args, missing_ok=False):
        code, out, err = self.run(["rclone"] + list(args))
        if code == 3 and missing_ok:  # rclone: directory not found
            return None
        if code:
            text = " ".join((err or b"").decode("utf-8", "replace").split())
            # Checked on the full stderr: the summary below cuts rclone's long errors.
            if "insufficient_space" in text.lower():
                raise DropboxFull("Dropbox is full (rclone: path/insufficient_space); %s; "
                                  "`review-kit share-gc` lists copies that can go"
                                  % self.usage())
            raise RuntimeError("rclone %s failed (exit %d)%s"
                               % (args[0], code, ("; " + text[:200]) if text else ""))
        return out

    def usage(self):
        """'used X of Y' from rclone about, or why it is unknown; never raises."""
        root = self.remote.split(":", 1)[0] + ":"
        code, out, _ = self.run(["rclone", "about", root, "--json"])
        try:
            about = json.loads(_text(out)) if code == 0 else None
            return "used %s of %s" % (_megabytes(about["used"]), _megabytes(about["total"]))
        except (ValueError, TypeError, KeyError):
            return "usage unknown (rclone about %s failed)" % root

    def _path(self, key):
        return self.remote + "/" + key if key else self.remote

    def put_file(self, key, path, content_type, digest):
        current = self.object_sha256(key)
        if current == digest:
            return False
        if current is not None:
            raise RuntimeError("Refusing to overwrite %s on Dropbox: it holds different bytes"
                               % key)
        self._copy(key, path)
        return True

    def _copy(self, key, path):
        self._rclone("copyto", str(path), self._path(key))

    def put_bytes(self, key, data, content_type, digest):
        name = _temporary_copy(data)
        try:
            return self.put_file(key, name, content_type, digest)
        finally:
            os.unlink(name)

    def replace_bytes(self, key, data, content_type, digest):
        name = _temporary_copy(data)
        try:
            self._copy(key, name)
        finally:
            os.unlink(name)

    def object_sha256(self, key):
        out = self._rclone("hashsum", "sha256", "--download", self._path(key), missing_ok=True)
        words = (out or b"").decode("utf-8", "replace").split()
        return words[0].lower() if words else None

    def sign_get(self, key, expires):
        lines = [line.strip() for line in
                 self._rclone("link", self._path(key)).decode("utf-8", "replace").splitlines()
                 if line.strip()]
        if not lines or not lines[-1].startswith(("https://", "http://")):
            raise RuntimeError("rclone link did not return a share URL")
        return lines[-1]

    def list_objects(self, prefix=""):
        prefix = prefix.strip("/")
        out = self._rclone("lsjson", "-R", "--files-only", self._path(prefix), missing_ok=True)
        try:
            items = json.loads((out or b"[]").decode("utf-8"))
        except ValueError:
            raise RuntimeError("rclone lsjson returned invalid JSON")
        return sorted(((prefix + "/" + item["Path"]) if prefix else item["Path"],
                       int(item.get("Size", 0))) for item in items)

    def delete_keys(self, keys):
        for key in keys:
            self._rclone("deletefile", self._path(key))
        if keys:
            self.run(["rclone", "rmdirs", "--leave-root", self.remote])


BACKENDS = ("supabase", "s3", "dropbox", "drive")


def create_backend(config, backend=None, run=None, hosts=None, host=None):
    """Storage backend by name; drive uses hosts.toml (host None = this Mac)."""
    selected = (backend or config.get("BACKEND", "supabase")).lower()
    if selected == "dropbox":
        return DropboxBackend(config, run=run)
    if selected == "drive":
        return DriveBackend(make_host(load_hosts() if hosts is None else hosts, host,
                                      run=run, config=config))
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
    raise ValueError("Backend must be one of: " + ", ".join(BACKENDS))
