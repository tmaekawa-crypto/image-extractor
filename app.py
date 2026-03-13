import html as _html
import io
import ipaddress
import os
import re
import socket
import subprocess
import sys
import zipfile
from typing import Optional
from urllib.parse import urljoin, urlparse

# Auto-install Playwright browser (runs once per container on Streamlit Cloud)
# System deps are handled by packages.txt; do NOT use --with-deps here
_pw_marker = os.path.expanduser("~/.cache/ms-playwright/.installed")
if not os.path.exists(_pw_marker):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False, capture_output=True,
        )
        if result.returncode == 0:
            os.makedirs(os.path.dirname(_pw_marker), exist_ok=True)
            open(_pw_marker, "w").close()
    except Exception:
        pass

import requests
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

# Playwright optional import
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ─────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Image Extractor",
    page_icon="🖼",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────
st.markdown(
    """
    <style>
        /* === Layout === */
        #MainMenu, header[data-testid="stHeader"], footer { display: none !important; }
        .block-container { padding: 1.5rem 2rem 2rem !important; max-width: 100% !important; }

        /* === Hero === */
        .hero-wrap { padding: 0.5rem 0 1.25rem; }
        .hero-title {
            font-size: 1.85rem;
            font-weight: 900;
            letter-spacing: -0.03em;
            background: linear-gradient(135deg, #e0e0f5 0%, #9580e8 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            line-height: 1.2;
        }
        .hero-sub { color: #585b73; font-size: 0.85rem; margin-top: 0.3rem; }

        /* === Extract button override === */
        button[data-testid="baseButton-primary"] {
            background: linear-gradient(135deg, #6d5ee8 0%, #9b6dff 100%) !important;
            border: none !important;
            font-weight: 700 !important;
            letter-spacing: 0.03em !important;
            box-shadow: 0 4px 24px rgba(109, 94, 232, 0.45) !important;
            transition: box-shadow 0.2s, transform 0.15s !important;
        }
        button[data-testid="baseButton-primary"]:hover {
            box-shadow: 0 6px 32px rgba(109, 94, 232, 0.65) !important;
            transform: translateY(-1px) !important;
        }

        /* === Panel header === */
        .panel-hdr {
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            color: #484a62;
            padding-bottom: 0.6rem;
            border-bottom: 1px solid #1e1f2c;
            margin-bottom: 0.75rem;
        }

        /* === Badge === */
        .badge {
            display: inline-block;
            background: rgba(124, 106, 247, 0.12);
            color: #9580e8;
            border: 1px solid rgba(124, 106, 247, 0.22);
            border-radius: 999px;
            padding: .15rem .65rem;
            font-size: .72rem;
            font-weight: 700;
            margin-left: .4rem;
        }
        .badge-g {
            background: rgba(34, 197, 94, 0.1);
            color: #4ade80;
            border-color: rgba(34, 197, 94, 0.2);
        }

        /* === List row === */
        .list-row {
            display: flex;
            align-items: center;
            border-radius: 8px;
            transition: background 0.12s;
        }
        .list-row:hover { background: #1a1b2a; }
        .list-row-active { background: #1f1a38 !important; }

        /* === Filename link buttons in list === */
        .list-row button[data-testid="baseButton-secondary"],
        .list-row ~ div button[data-testid="baseButton-secondary"] {
            background: transparent !important;
            border: none !important;
            color: #b8bace !important;
            font-size: 0.8rem !important;
            text-align: left !important;
            padding: 0.2rem 0 !important;
            font-weight: 400 !important;
            box-shadow: none !important;
        }
        .list-row-active button[data-testid="baseButton-secondary"] {
            color: #c4b5fd !important;
        }

        /* === Rename card === */
        .rename-card {
            background: #14151e;
            border: 1px solid #23243a;
            border-radius: 12px;
            padding: 1rem 1.25rem;
            margin-bottom: 1rem;
        }
        .rename-thumb-label {
            font-size: .75rem;
            color: #585b73;
            word-break: break-all;
            margin: .4rem 0 .6rem;
        }

        /* === Divider === */
        hr { border-color: #1e1f2c !important; margin: 1rem 0 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
TIMEOUT = 15
MAX_RESPONSE_BYTES = 20 * 1024 * 1024   # 20 MB — page HTML / full image DL
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024   # 2 MB  — thumbnail preview
MAX_IMAGES = 500                          # cap on extracted images per page

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _is_private_host(host: str) -> bool:
    """True if host is a private/loopback/link-local IP literal."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


def _validate_url(url: str) -> Optional[str]:
    """Return error message if URL is invalid/unsafe, else None.
    Performs actual DNS resolution to block DNS-rebinding attacks."""
    try:
        p = urlparse(url)
    except Exception:
        return "URLの形式が正しくありません。"
    if p.scheme not in ("http", "https"):
        return "HTTPまたはHTTPSのURLのみ対応しています。"
    host = p.hostname or ""
    if _is_private_host(host):
        return "プライベートIPアドレスへのアクセスは許可されていません。"
    # Resolve hostname → check all returned IPs (DNS-rebinding protection)
    try:
        addrs = socket.getaddrinfo(host, None)
        for _, _, _, _, sockaddr in addrs:
            if _is_private_host(sockaddr[0]):
                return "プライベートIPアドレスへのアクセスは許可されていません。"
    except socket.gaierror:
        return "ホスト名を解決できませんでした。URLを確認してください。"
    return None


@st.cache_data(show_spinner=False, ttl=300)
def _is_hostname_safe(hostname: str) -> bool:
    """DNS-based safety check for a hostname; result cached 5 min to amortize
    the cost across many images from the same host."""
    if _is_private_host(hostname):
        return False
    try:
        addrs = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in addrs:
            if _is_private_host(sockaddr[0]):
                return False
    except socket.gaierror:
        return False  # unresolvable host — reject
    return True


def _is_safe_img_url(url: str) -> bool:
    """Safety check for extracted image URLs (scheme + cached DNS check)."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        return _is_hostname_safe(p.hostname or "")
    except Exception:
        return False


def _sanitize_filename(name: str) -> str:
    """Strip null bytes, control characters, and path components from a filename."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return os.path.basename(name) or "image"


def _safe_error(e: Exception) -> str:
    """Return a user-safe error message (redacts credentials embedded in URLs)."""
    msg = str(e)
    return re.sub(r"(https?://)([^@\s]+@)", r"\1[redacted]@", msg)


def _download_bytes(url: str, max_bytes: int = MAX_RESPONSE_BYTES) -> Optional[bytes]:
    """Stream-download with a configurable byte cap. Returns None if over limit."""
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
    r.raise_for_status()
    chunks: list[bytes] = []
    total = 0
    for chunk in r.iter_content(8192):
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@st.cache_data(show_spinner=False)
def fetch_thumbnail_cached(url: str) -> Optional[bytes]:
    """Download and shrink to thumbnail with a 2 MB cap; cached per URL."""
    try:
        data = _download_bytes(url, max_bytes=MAX_THUMBNAIL_BYTES)
        if data is None:
            return None
        pil = Image.open(io.BytesIO(data))
        pil.thumbnail((80, 60))
        buf = io.BytesIO()
        fmt = "PNG" if pil.mode in ("RGBA", "P") else "JPEG"
        pil.save(buf, format=fmt)
        return buf.getvalue()
    except (requests.RequestException, UnidentifiedImageError, OSError):
        return None


@st.cache_data(show_spinner=False)
def fetch_image_bytes(url: str) -> Optional[bytes]:
    """Download image bytes with 20 MB limit; cached per URL."""
    try:
        return _download_bytes(url)
    except requests.RequestException:
        return None


def _fetch_html_playwright(page_url: str) -> str:
    """Use Playwright headless browser to get fully-rendered HTML.
    Uses domcontentloaded + 8s networkidle fallback to avoid hanging on
    pages with long-polling connections."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(extra_http_headers=HEADERS)
            page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
            # Give dynamic content up to 8 s to settle; ignore if page keeps polling
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            return page.content()
        finally:
            browser.close()


@st.cache_data(show_spinner=False, ttl=60)
def fetch_image_urls(page_url: str, use_playwright: bool = False) -> list[dict]:
    """Return up to MAX_IMAGES {url, filename} dicts from <img> tags on the page."""
    if use_playwright:
        html = _fetch_html_playwright(page_url)
    else:
        raw = _download_bytes(page_url)
        if raw is None:
            raise ValueError("ページのHTMLが大きすぎます（20MB上限）")
        html = raw.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    images: list[dict] = []

    for tag in soup.find_all("img"):
        if len(images) >= MAX_IMAGES:
            break
        raw_src = (
            tag.get("src")
            or tag.get("data-src")
            or tag.get("data-lazy-src")
            or tag.get("data-original")
            or ""
        ).strip()
        if not raw_src or raw_src.startswith("data:"):
            continue
        abs_url = urljoin(page_url, raw_src)
        # SSRF: skip private/invalid image URLs (cached DNS check)
        if not _is_safe_img_url(abs_url):
            continue
        # Dedup by full URL (query params preserved — CDN variants treated as distinct)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        path = urlparse(abs_url).path
        filename = os.path.basename(path) or "image"
        if not re.search(r"\.(jpe?g|png|gif|webp|svg|bmp|ico)$", filename, re.I):
            filename += ".jpg"
        images.append({"url": abs_url, "filename": filename})

    return images


@st.cache_data(show_spinner=False)
def make_zip_cached(items_tuple: tuple) -> tuple:
    """Pack selected images into ZIP. Returns (zip_bytes, actual_count).
    Sanitizes filenames; deduplicates collisions; cached by content."""
    buf = io.BytesIO()
    actual_count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names: set[str] = set()
        for url, save_as in items_tuple:
            # Sanitize: strip null bytes, control chars, and path components
            safe_name = _sanitize_filename(save_as) or "image.jpg"
            # Deduplicate: append _1, _2, ... on collision
            final_name = safe_name
            if final_name in used_names:
                stem, ext = (safe_name.rsplit(".", 1) if "." in safe_name
                             else (safe_name, ""))
                counter = 1
                while final_name in used_names:
                    final_name = f"{stem}_{counter}.{ext}" if ext else f"{stem}_{counter}"
                    counter += 1
            used_names.add(final_name)
            data = fetch_image_bytes(url)
            if data:
                zf.writestr(final_name, data)
                actual_count += 1
    buf.seek(0)
    return buf.read(), actual_count


# module-level checkbox callback (avoids per-rerun function definition in loop)
def _on_check(url: str) -> None:
    if st.session_state[f"chk_{url}"]:
        st.session_state.active_url = url


# ─────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────
for _k, _v in [
    ("images", []),
    ("selected", {}),
    ("save_as", {}),
    ("active_url", None),
    ("zip_name", "images.zip"),
    ("last_page_url", ""),
    ("use_playwright", False),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ─────────────────────────────────────────────
# Hero
# ─────────────────────────────────────────────
st.markdown(
    """
    <div class="hero-wrap">
        <div class="hero-title">🖼 Image Extractor</div>
        <div class="hero-sub">WebページのURLを入力して、画像を一括抽出・ダウンロード</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────
# URL Input
# ─────────────────────────────────────────────
col_url, col_btn = st.columns([5, 1])
with col_url:
    page_url = st.text_input(
        "URL",
        placeholder="https://example.com/gallery",
        label_visibility="collapsed",
    )
with col_btn:
    extract_clicked = st.button("⚡ 抽出する", type="primary", use_container_width=True)

if PLAYWRIGHT_AVAILABLE:
    st.toggle(
        "🌐 **JSレンダリングモード（Playwright）**",
        key="use_playwright",
        help="JavaScriptで動的に生成されるSPAページ対応。通常より5〜10秒かかります。",
    )
else:
    st.caption(
        "⚠️ Playwrightが未インストール — JSモード無効  "
        "（`pip install playwright && python -m playwright install chromium`）"
    )

st.markdown("<hr style='margin:.75rem 0 1.25rem'>", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────
if extract_clicked and page_url:
    # SSRF: validate URL (scheme + DNS resolution) before any request
    url_err = _validate_url(page_url)
    if url_err:
        st.error(url_err)
    else:
        use_pw = st.session_state.use_playwright
        auto_retried = False
        imgs = []
        error = None
        try:
            with st.spinner("JSレンダリング中… しばらくお待ちください" if use_pw else "画像を抽出中…"):
                imgs = fetch_image_urls(page_url, use_playwright=use_pw)
            # Auto-fallback: 通常モードで0件 → Playwrightで再確認
            if not imgs and not use_pw and PLAYWRIGHT_AVAILABLE:
                with st.spinner("JSレンダリングモードで確認中…"):
                    imgs = fetch_image_urls(page_url, use_playwright=True)
                auto_retried = True
        except requests.RequestException as e:
            error = _safe_error(e)
        except Exception as e:
            error = _safe_error(e)

        if error:
            st.error(f"取得エラー: {error}")
        else:
            st.session_state.images = imgs
            st.session_state.selected = {i["url"]: False for i in imgs}
            st.session_state.save_as = {i["url"]: i["filename"] for i in imgs}
            st.session_state.active_url = None
            st.session_state.zip_name = "images.zip"
            st.session_state.last_page_url = page_url
            if imgs:
                suffix = f"（{MAX_IMAGES}件上限で打ち切り）" if len(imgs) == MAX_IMAGES else ""
                if auto_retried:
                    st.success(f"{len(imgs)} 枚検出（JSレンダリングモードで取得）{suffix}")
                else:
                    st.success(f"{len(imgs)} 枚の画像を検出しました。{suffix}")
            else:
                msg = "画像が見つかりませんでした。"
                if not PLAYWRIGHT_AVAILABLE and not use_pw:
                    msg += "  \nPlaywrightをインストールするとSPAページにも対応できます。"
                st.warning(msg)

# ─────────────────────────────────────────────
# Main layout
# ─────────────────────────────────────────────
images: list[dict] = st.session_state.images

if images:
    left_col, right_col = st.columns([4, 6], gap="large")

    # ─── LEFT: Image list ───────────────────
    with left_col:
        n_sel = sum(st.session_state.selected.values())
        st.markdown(
            f"<div class='panel-hdr'>画像一覧"
            f"<span class='badge'>{len(images)}</span>"
            f"<span class='badge badge-g'>{n_sel} 選択</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        ta, tb = st.columns(2)
        with ta:
            if st.button("すべて選択", use_container_width=True):
                for k in st.session_state.selected:
                    st.session_state.selected[k] = True
                    st.session_state[f"chk_{k}"] = True
        with tb:
            if st.button("選択解除", use_container_width=True):
                for k in st.session_state.selected:
                    st.session_state.selected[k] = False
                    st.session_state[f"chk_{k}"] = False

        list_height = min(600, max(300, len(images) * 56))
        with st.container(height=list_height, border=False):
            for img in images:
                url = img["url"]
                is_active = st.session_state.active_url == url
                row_cls = "list-row list-row-active" if is_active else "list-row"

                st.markdown(f"<div class='{row_cls}'>", unsafe_allow_html=True)
                c_thumb, c_name, c_check = st.columns([1, 5, 1])

                with c_thumb:
                    thumb = fetch_thumbnail_cached(url)
                    if thumb:
                        st.image(thumb, use_container_width=True)
                    else:
                        st.markdown(
                            "<div style='height:40px;background:#1e1f2c;border-radius:4px;"
                            "display:flex;align-items:center;justify-content:center;font-size:1.1rem'>🖼</div>",
                            unsafe_allow_html=True,
                        )

                with c_name:
                    fname = st.session_state.save_as.get(url, img["filename"])
                    display = fname if len(fname) <= 28 else fname[:25] + "…"
                    if st.button(display, key=f"act_{url}", use_container_width=True):
                        st.session_state.active_url = None if is_active else url
                        st.rerun()

                with c_check:
                    st.checkbox(
                        "",
                        value=st.session_state.selected.get(url, False),
                        key=f"chk_{url}",
                        on_change=_on_check,
                        args=(url,),
                    )
                    st.session_state.selected[url] = st.session_state[f"chk_{url}"]

                st.markdown("</div>", unsafe_allow_html=True)

    # ─── RIGHT: Rename + Download ────────────
    with right_col:
        # ── Rename card ──────────────────────
        aurl = st.session_state.active_url
        st.markdown("<div class='rename-card'>", unsafe_allow_html=True)
        st.markdown("<div class='panel-hdr'>ファイル名設定</div>", unsafe_allow_html=True)

        if aurl:
            rc1, rc2 = st.columns([1, 4])
            with rc1:
                athumb = fetch_thumbnail_cached(aurl)
                if athumb:
                    st.image(athumb, use_container_width=True)
            with rc2:
                # XSS: escape URL-derived text before HTML injection
                safe_label = _html.escape(aurl.split("/")[-1].split("?")[0])
                st.markdown(
                    f"<div class='rename-thumb-label'>{safe_label}</div>",
                    unsafe_allow_html=True,
                )
                new_name = st.text_input(
                    "新しいファイル名",
                    value=st.session_state.save_as.get(aurl, ""),
                    key=f"name_{aurl}",
                    label_visibility="collapsed",
                )
                st.session_state.save_as[aurl] = new_name
        else:
            st.markdown(
                "<div style='color:#35374d;font-size:.82rem;padding:.25rem 0'>"
                "左のチェックボックスかファイル名をクリックして選択</div>",
                unsafe_allow_html=True,
            )

        st.markdown("</div>", unsafe_allow_html=True)

        # ── Download ─────────────────────────
        st.markdown("<div class='panel-hdr'>ダウンロード</div>", unsafe_allow_html=True)

        selected_imgs = [
            (img["url"], st.session_state.save_as.get(img["url"], img["filename"]))
            for img in images
            if st.session_state.selected.get(img["url"])
        ]

        n = len(selected_imgs)
        if n == 0:
            st.markdown(
                "<div style='color:#35374d;font-size:.82rem'>"
                "左のチェックボックスで画像を選択してください</div>",
                unsafe_allow_html=True,
            )
        elif n == 1:
            dl_url, dl_name = selected_imgs[0]
            # Sanitize filename for single-file download (path traversal prevention)
            dl_name_safe = _sanitize_filename(dl_name) or "image"
            data = fetch_image_bytes(dl_url)
            if data:
                st.download_button(
                    label=f"⬇ {dl_name_safe}",
                    data=data,
                    file_name=dl_name_safe,
                    mime="application/octet-stream",
                    type="primary",
                    use_container_width=True,
                )
        else:
            # zip_name_input widget syncs to st.session_state.zip_name manually
            zname = st.text_input(
                "ZIPファイル名",
                value=st.session_state.zip_name,
                key="zip_name_input",
            )
            if not zname.lower().endswith(".zip"):
                zname += ".zip"
            st.session_state.zip_name = zname
            zip_bytes, actual_count = make_zip_cached(tuple(selected_imgs))
            # Show actual count in case some images failed to download
            count_label = (f"{actual_count}/{n} 枚" if actual_count < n
                           else f"{n} 枚")
            st.download_button(
                label=f"⬇ {count_label}をZIPでダウンロード",
                data=zip_bytes,
                file_name=zname,
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
