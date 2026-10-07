import base64
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATUS_HTML = HERE / "status.html"
NGINX_CONF = HERE / "nginx.conf"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)
_CSP_SCRIPT_RE = re.compile(r"script-src 'sha256-[^']*'")
_CSP_STYLE_RE = re.compile(r"style-src 'sha256-[^']*'")


def _sha256_b64(text: str) -> str:
    return base64.b64encode(
        hashlib.sha256(text.encode("utf-8")).digest()
    ).decode()


def compute() -> tuple[str, str]:
    html = _COMMENT_RE.sub("", STATUS_HTML.read_text(encoding="utf-8"))
    scripts = _SCRIPT_RE.findall(html)
    styles = _STYLE_RE.findall(html)
    if len(scripts) != 1 or len(styles) != 1:
        raise SystemExit(
            "status.html must contain exactly one <script> and one <style> "
            "block"
        )
    return _sha256_b64(scripts[0]), _sha256_b64(styles[0])


def expected_conf(conf: str, script_hash: str, style_hash: str) -> str:
    conf = _CSP_SCRIPT_RE.sub(f"script-src 'sha256-{script_hash}'", conf)
    return _CSP_STYLE_RE.sub(f"style-src 'sha256-{style_hash}'", conf)


def main(argv: list[str]) -> int:
    script_hash, style_hash = compute()
    conf = NGINX_CONF.read_text(encoding="utf-8")
    updated = expected_conf(conf, script_hash, style_hash)
    if "--write" in argv:
        NGINX_CONF.write_text(updated, encoding="utf-8")
        print("nginx.conf updated")
        return 0
    if "--check" in argv:
        if updated != conf:
            print(
                "status.html CSP hashes are out of date: "
                "run python3 infra/gateway/csp_hashes.py --write"
            )
            return 1
        print("status.html CSP hashes are up to date")
        return 0
    print(f"script-src 'sha256-{script_hash}'")
    print(f"style-src 'sha256-{style_hash}'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
