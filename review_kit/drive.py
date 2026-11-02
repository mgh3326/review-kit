"""Google Drive upload state through macOS File Provider, locally or over ssh.

A file copied under the Drive mount (``~/Library/CloudStorage/GoogleDrive-*``)
appears at once but may still be uploading.  ``fileproviderctl evaluate <file>``
reports ``isUploaded`` and ``isUploading``; this module runs it on the local
Mac or on a configured mount host and classifies each file.
"""
import contextlib
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess  # nosec B404 -- argv lists only, never shell=True


UPLOADED = "uploaded"
UPLOADING = "uploading"
NOT_UPLOADED = "not-uploaded"
UNKNOWN = "unknown"
STATUSES = (UPLOADED, UPLOADING, NOT_UPLOADED, UNKNOWN)
HOSTS_FILE = "hosts.toml"
HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SSH_DEST = re.compile(r"^(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.:%\[\]-]+$")
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
TRUE_WORDS = frozenset(("1", "true", "yes"))
FALSE_WORDS = frozenset(("0", "false", "no"))


def _flag(text, name):
    """True/False for 'name = 1', 'name: true', '"name" : NO' and similar; None if absent."""
    pattern = r"\b%s\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9]+)" % re.escape(name)
    values = [match.group(1).lower() for match in re.finditer(pattern, text)]
    if not values:
        return None
    if any(value in TRUE_WORDS for value in values):
        return True
    if all(value in FALSE_WORDS for value in values):
        return False
    return None


def parse_evaluate(text):
    """(status, detail) from ``fileproviderctl evaluate`` output.

    A file counts as uploaded only when isUploaded is true and isUploading is
    not; output without either key is ``unknown``, never ``uploaded``.
    """
    uploaded = _flag(text, "isUploaded")
    uploading = _flag(text, "isUploading")
    if uploaded is None and uploading is None:
        return UNKNOWN, "fileproviderctl output has no isUploaded/isUploading"
    if uploading:
        return UPLOADING, ""
    if uploaded:
        return UPLOADED, ""
    return NOT_UPLOADED, ""


def _decode(data):
    return (data or b"").decode("utf-8", "replace")


# Long enough for an rclone/rsync copy of a large package; a hung fileproviderctl,
# ssh or rclone still ends, so the --wait deadline and share-gc cannot block forever.
COMMAND_TIMEOUT_SECONDS = 3600


def default_run(argv, stdin=None, timeout=COMMAND_TIMEOUT_SECONDS):
    """(returncode, stdout, stderr) for argv; a missing program is returncode 127,
    one that runs past timeout is killed and returns 124."""
    try:
        result = subprocess.run([str(arg) for arg in argv], input=stdin,  # nosec B603
                                capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, b"", ("%s: command not found" % argv[0]).encode("utf-8")
    except subprocess.TimeoutExpired:
        return 124, b"", ("%s: timed out after %ss" % (argv[0], timeout)).encode("utf-8")
    return result.returncode, result.stdout, result.stderr


def _short(data, limit=200):
    return " ".join(_decode(data).split())[:limit]


def remote_quote(path):
    """Shell-quote one remote argument, expanding a leading ~ through $HOME."""
    path = str(path)
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


# --- hosts.toml ---------------------------------------------------------------

def _toml_string(raw, line_number):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        body, out, index = raw[1:-1], [], 0
        escapes = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}
        while index < len(body):
            char = body[index]
            if char == "\\" and index + 1 < len(body) and body[index + 1] in escapes:
                out.append(escapes[body[index + 1]])
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)
    if re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    if raw in ("true", "false"):
        return raw == "true"
    raise ValueError("hosts.toml line %d: values must be quoted strings, integers or booleans"
                     % line_number)


def _strip_comment(line):
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote and (quote == "'" or line[index - 1] != "\\"):
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#":
            return line[:index]
    return line


def parse_hosts_toml(text):
    """The small TOML subset hosts.toml needs: [tables], [a.b] and key = value lines."""
    data, table = {}, None
    for number, line in enumerate(text.splitlines(), 1):
        line = _strip_comment(line).strip()
        if not line:
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[["):
                raise ValueError("hosts.toml line %d: unsupported table header" % number)
            table = data
            for part in line[1:-1].split("."):
                part = part.strip().strip('"')
                if not HOST_NAME.fullmatch(part):
                    raise ValueError("hosts.toml line %d: bad table name" % number)
                table = table.setdefault(part, {})
            continue
        if "=" not in line:
            raise ValueError("hosts.toml line %d: expected key = value" % number)
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("hosts.toml line %d: bad key" % number)
        (table if table is not None else data)[key] = _toml_string(value, number)
    return data


def hosts_path(config_dir=None):
    root = config_dir or os.environ.get("REVIEW_KIT_CONFIG_DIR") or str(
        Path.home() / ".config" / "review-kit")
    return Path(root).expanduser() / HOSTS_FILE


def load_hosts(path=None):
    """{'local': {...}, 'hosts': {name: {...}}} from hosts.toml; empty when absent."""
    path = Path(path) if path else hosts_path()
    if not path.is_file():
        return {"local": {}, "hosts": {}, "_path": str(path)}
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
        data = tomllib.loads(text)
    except ImportError:
        data = parse_hosts_toml(text)
    except ValueError as exc:  # tomllib.TOMLDecodeError is a ValueError
        raise ValueError("hosts.toml is not valid TOML: %s" % exc)
    hosts = data.get("hosts", {})
    local = data.get("local", {})
    if not isinstance(hosts, dict) or not isinstance(local, dict):
        raise ValueError("hosts.toml: [hosts.<name>] and [local] must be tables")
    return {"local": local, "hosts": hosts, "_path": str(path)}


# --- hosts --------------------------------------------------------------------

class LocalHost:
    """The machine this command runs on."""

    label = "local"

    def __init__(self, entry=None, run=None, drive_root=None):
        entry = entry or {}
        self.run = run or default_run
        root = drive_root or entry.get("drive_root") or ""
        self.drive_root = os.path.expanduser(root) if root else ""
        self.fileproviderctl = (os.environ.get("REVIEW_KIT_FILEPROVIDERCTL") or
                                entry.get("fileproviderctl") or "fileproviderctl")

    def join(self, *parts):
        return str(Path(*parts))

    def resolve(self, path):
        """Status paths: absolute, ~ or relative to the current directory."""
        return str(Path(os.path.expanduser(str(path))).absolute())

    def evaluate(self, path):
        if not os.path.lexists(path):
            return UNKNOWN, "no such file"
        return self._evaluate_output(self.run([self.fileproviderctl, "evaluate", path]))

    @staticmethod
    def _evaluate_output(result):
        code, out, err = result
        if code == 127:
            return UNKNOWN, "fileproviderctl not found (macOS File Provider only)"
        if code:
            return UNKNOWN, "fileproviderctl exit %d: %s" % (code, _short(err or out))
        return parse_evaluate(_decode(out))

    @staticmethod
    def _walk(path):
        for root, directories, names in os.walk(path):
            # Hidden folders (.git, .Trash, ...) are pruned, like the remote find below.
            directories[:] = sorted(d for d in directories if not d.startswith("."))
            for name in sorted(names):
                if not name.startswith("."):
                    yield os.path.join(root, name)

    def files_under(self, path):
        return list(self._walk(path)) if os.path.isdir(path) else [path]

    def list_sizes(self, path):
        return [(found, os.path.getsize(found)) for found in self._walk(path)]

    def sha256(self, path):
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return None
        return digest.hexdigest()

    def put_file(self, source, target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))

    def exists(self, path):
        return os.path.lexists(path)

    def symlinks(self, paths):
        return [path for path in paths if os.path.islink(path)]

    def remove(self, paths):
        for path in paths:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


class SshHost(LocalHost):
    """A Mac that mounts Drive, reached with ssh (and rsync for copies)."""

    def __init__(self, name, entry, run=None):
        destination = str(entry.get("ssh") or "").strip()
        if not destination or destination.startswith("-") or not SSH_DEST.fullmatch(destination):
            raise ValueError("hosts.toml [hosts.%s] needs ssh = \"[user@]host\"" % name)
        self.label = name
        self.destination = destination
        self.run_local = run or default_run
        self.drive_root = str(entry.get("drive_root") or "")
        self.fileproviderctl = str(entry.get("fileproviderctl") or "fileproviderctl")

    def ssh(self, argv, stdin=None):
        command = " ".join(remote_quote(arg) for arg in argv)
        return self.run_local(["ssh"] + list(SSH_OPTIONS) + [self.destination, command],
                              stdin=stdin)

    def _checked(self, argv, stdin=None):
        code, out, err = self.ssh(argv, stdin=stdin)
        if code:
            raise RuntimeError("%s on %s failed (exit %d): %s"
                               % (argv[0], self.label, code, _short(err)))
        return out

    def join(self, *parts):
        return str(PurePosixPath(*parts))

    def resolve(self, path):
        """Remote status paths: absolute, ~ or relative to drive_root (else $HOME)."""
        path = str(path)
        if path.startswith(("/", "~")) or not self.drive_root:
            return path
        return str(PurePosixPath(self.drive_root) / path)

    def evaluate(self, path):
        code, out, err = self.ssh([self.fileproviderctl, "evaluate", path])
        if code == 255:
            return UNKNOWN, "ssh to %s failed: %s" % (self.label, _short(err))
        return self._evaluate_output((code, out, err))

    def _find(self, path, *extra):
        # Below path, any entry named .* is skipped, and a hidden folder is not entered.
        action = list(extra) or ["-print"]
        code, out, err = self.ssh(["find", path, "-mindepth", "1", "-name", ".*", "-prune",
                                   "-o", "-type", "f"] + action)
        if code == 255:
            raise RuntimeError("ssh to %s failed: %s" % (self.label, _short(err)))
        return code, sorted(line for line in _decode(out).splitlines() if line)

    def files_under(self, path):
        try:
            code, lines = self._find(path)
        except RuntimeError:
            return [path]
        return lines if code == 0 and lines else [path]

    def list_sizes(self, path):
        code, lines = self._find(path, "-exec", "stat", "-f", "%z %N", "{}", "+")
        sizes = []
        for line in lines if code == 0 else []:
            size, _, found = line.partition(" ")
            if size.isdigit() and found:
                sizes.append((found, int(size)))
        return sorted(sizes, key=lambda item: item[0])

    def sha256(self, path):
        code, out, _ = self.ssh(["shasum", "-a", "256", path])
        first = _decode(out).split()
        return first[0].lower() if code == 0 and first else None

    def put_file(self, source, target):
        parent, name = str(PurePosixPath(target).parent), PurePosixPath(target).name
        if Path(source).name != name:
            self._checked(["mkdir", "-p", parent])
            self._checked(["sh", "-c", 'cat > "$1"', "sh", target],
                          stdin=Path(source).read_bytes())
            return
        # --rsync-path runs through the remote shell in every rsync version, so the
        # quoted mkdir/cd handle spaces in Drive paths without rsync's own arg escaping.
        quoted = remote_quote(parent)
        argv = ["rsync", "-t", "-e", "ssh " + " ".join(SSH_OPTIONS),
                "--rsync-path", "mkdir -p %s && cd %s && rsync" % (quoted, quoted),
                str(Path(source).absolute()), self.destination + ":./"]
        code, _, err = self.run_local(argv)
        if code:
            raise RuntimeError("rsync to %s failed (exit %d): %s"
                               % (self.label, code, _short(err)))

    def symlinks(self, paths):
        script = 'for p in "$@"; do if [ -L "$p" ]; then printf "%s\\n" "$p"; fi; done'
        out = self._checked(["sh", "-c", script, "sh"] + list(paths)) if paths else b""
        return [line for line in _decode(out).splitlines() if line]

    def exists(self, path):
        code, _, err = self.ssh(["test", "-e", path, "-o", "-L", path])
        if code not in (0, 1):
            raise RuntimeError("test on %s failed (exit %d): %s" % (self.label, code, _short(err)))
        return code == 0

    def remove(self, paths):
        if paths:
            self._checked(["rm", "-f", "--"] + list(paths))


# The profile key the earlier `deliver` command used: a local path or [user@]host:/path.
MOUNT_SSH_TARGET = re.compile(r"^((?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]{2,}):(.+)$")
PROFILE_MOUNT = "DRIVE_MOUNT_DIR"


def make_host(hosts, name=None, run=None, config=None):
    """The default Drive host for no name, else the SshHost configured as [hosts.<name>].

    The default is the profile's DRIVE_MOUNT_DIR when set (a local path, or an ssh
    target host:/path as `deliver` accepted), else [local] in hosts.toml.
    """
    config = config or {}
    hosts = hosts or {}
    if not name or name == "local":
        mount = (config.get(PROFILE_MOUNT) or "").strip()
        target = MOUNT_SSH_TARGET.match(mount)
        if target:
            return SshHost(PROFILE_MOUNT, {"ssh": target.group(1), "drive_root": target.group(2)},
                           run=run)
        return LocalHost(hosts.get("local"), run=run, drive_root=mount)
    if not HOST_NAME.fullmatch(name):
        raise ValueError("Host names may use letters, digits, dot, underscore and dash")
    entry = hosts.get("hosts", {}).get(name)
    if not isinstance(entry, dict):
        raise ValueError("Unknown host %r; add [hosts.%s] with ssh = \"...\" to %s"
                         % (name, name, hosts.get("_path", HOSTS_FILE)))
    return SshHost(name, entry, run=run)


def drive_status(host, paths, expand=True):
    """{'host', 'all_uploaded', 'files': [{'path', 'status', 'detail'}]} for paths."""
    files = []
    for path in paths:
        targets = host.files_under(path) if expand else [path]
        for target in targets:
            status, detail = host.evaluate(target)
            files.append({"path": target, "status": status, "detail": detail})
    return {"host": host.label, "files": files,
            "all_uploaded": bool(files) and all(item["status"] == UPLOADED for item in files)}


def summarize(files):
    counts = {}
    for item in files:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return ", ".join("%d %s" % (counts[status], status) for status in STATUSES
                     if counts.get(status))
