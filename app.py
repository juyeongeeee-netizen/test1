"""쇼핑리스트 앱 (Streamlit)."""

import hmac
import html
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

DATA_FILE = Path(__file__).parent / "data" / "items.json"
REFRESH_SECONDS = 10  # 다른 탭/기기의 변경을 화면에 반영하는 주기 (저장은 항상 최신 파일 기준이라 데이터 손실과는 무관)


# ---------- 데이터 ----------
# data/items.json이 유일한 기준 데이터다. 세션은 파일을 캐시하지 않고 매 실행마다 다시 읽으며,
# 모든 변경은 잠금 안에서 "파일 읽기 → 수정 → 저장"으로 처리한다. (여러 탭이 서로 덮어쓰지 않게)

@st.cache_resource
def _file_lock() -> threading.Lock:
    """서버 프로세스 전체(모든 세션)가 공유하는 파일 잠금."""
    return threading.Lock()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_item(name: str, quantity: int = 1) -> dict:
    """PRD 4.2 데이터 모델에 맞는 새 아이템을 만든다."""
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "quantity": quantity,
        "checked": False,
        "created_at": now_iso(),
    }


def _normalize(raw: object, seen_ids: set[str]) -> dict | None:
    """파일에서 읽은 항목 하나를 데이터 모델에 맞게 고친다. 살릴 수 없으면 None."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    try:
        quantity = max(1, int(raw.get("quantity", 1)))
    except (TypeError, ValueError):
        quantity = 1
    item_id = str(raw.get("id") or "")
    if not item_id or item_id in seen_ids:  # id 누락/중복은 위젯 key 충돌을 일으키므로 새로 발급
        item_id = str(uuid.uuid4())
    seen_ids.add(item_id)
    return {
        "id": item_id,
        "name": name,
        "quantity": quantity,
        "checked": raw.get("checked") is True,
        "created_at": str(raw.get("created_at") or now_iso()),
    }


def _quarantine() -> Path:
    """손상된 데이터 파일을 지우지 않고 옆에 보관한다."""
    backup = DATA_FILE.with_name(f"items.corrupt-{datetime.now():%Y%m%d-%H%M%S}.json")
    os.replace(DATA_FILE, backup)
    return backup


def _write(items: list[dict]) -> None:
    """임시 파일에 쓴 뒤 교체한다. 쓰는 도중 중단돼도 기존 파일이 깨지지 않는다."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_FILE.parent, prefix=".items-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read() -> tuple[list[dict], str | None]:
    """파일에서 아이템을 읽는다. (잠금 안에서 호출) 문제가 있으면 복구하고 안내 문구를 함께 돌려준다."""
    if not DATA_FILE.exists():
        return [], None
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("최상위 값이 리스트가 아님")
    except (UnicodeDecodeError, ValueError):  # JSONDecodeError는 ValueError의 하위 클래스
        backup = _quarantine()
        return [], f"데이터 파일이 손상되어 빈 리스트로 시작해요. 원본은 `{backup.name}`에 보관했어요."

    seen: set[str] = set()
    items = [it for it in (_normalize(raw, seen) for raw in data) if it]
    notice = None
    if items != data:
        skipped = len(data) - len(items)
        notice = (f"형식이 잘못된 항목 {skipped}개를 건너뛰었어요." if skipped
                  else "데이터 파일의 일부 항목을 올바른 형식으로 고쳤어요.")
        _write(items)  # 고친 내용을 저장해 두어야 새로 발급한 id가 매번 바뀌지 않는다
    return items, notice


def file_signature() -> tuple[int, int] | None:
    try:
        stat = DATA_FILE.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def refresh_items() -> None:
    """화면을 그리기 전에 항상 파일에서 최신 목록을 읽는다."""
    try:
        with _file_lock():
            items, notice = _read()
            st.session_state["data_sig"] = file_signature()
    except OSError:
        st.session_state["error"] = "데이터를 불러오지 못했어요. 잠시 후 새로고침해 주세요."
        st.session_state.setdefault("items", [])
        return
    st.session_state["items"] = items
    if notice:
        st.session_state["notice"] = notice


def mutate(change) -> None:
    """최신 파일 기준으로 change(items)를 적용하고 저장한다. change는 토스트 문구를 돌려줄 수 있다."""
    try:
        with _file_lock():
            items, notice = _read()
            toast = change(items)
            _write(items)
    except OSError:
        st.session_state["error"] = "저장하지 못했어요. 잠시 후 다시 시도해 주세요."
        return
    if notice:
        st.session_state["notice"] = notice
    if toast:
        st.session_state["toast"] = toast


def find_item(items: list[dict], item_id: str) -> dict | None:
    return next((i for i in items if i["id"] == item_id), None)


def init_state() -> None:
    for key in ("editing_id", "toast", "notice", "error", "add_warning", "edit_warning"):
        st.session_state.setdefault(key, None)
    st.session_state.setdefault("authed", False)


# ---------- 텍스트 이스케이프 ----------

_MD_PUNCT = re.compile(r"([!-/:-@\[-`{-~])")  # ASCII 문장부호 전체


def md_escape(text: str) -> str:
    """마크다운으로 렌더링되는 곳(토스트, 위젯 라벨)에 사용자 입력을 그대로 보이게 한다.
    ':'와 '/'까지 이스케이프하므로 URL 자동 링크도 생기지 않는다."""
    return _MD_PUNCT.sub(r"\\\1", text)


def toast_name(text: str) -> str:
    """토스트용 이름. 토스트는 이스케이프와 별개로 URL/도메인/이메일을 자동 링크로 바꾸므로
    '.', ':', '@' 앞뒤에 보이지 않는 연결 문자(U+2060)를 넣어 링크로 인식되지 않게 한다."""
    return re.sub(r"(\\[.:@])", "⁠\\1⁠", md_escape(text))  # 이스케이프된 "\." 묶음 바깥에 넣는다


# ---------- 콜백 ----------
# 리스트 변경은 렌더링 도중이 아니라 콜백에서만 처리한다.

def on_add() -> None:
    name = st.session_state["new_name"].strip()
    quantity = int(st.session_state["new_qty"])
    if not name:
        st.session_state["add_warning"] = "아이템 이름을 입력해 주세요."
        return
    st.session_state["add_warning"] = None

    def change(items: list[dict]) -> str:
        items.append(create_item(name, quantity))
        return f"🛒 {toast_name(name)} 추가됨"

    mutate(change)


def on_toggle(item_id: str) -> None:
    checked = st.session_state[f"chk_{item_id}"]

    def change(items: list[dict]) -> None:
        item = find_item(items, item_id)
        if item:
            item["checked"] = checked

    mutate(change)


def on_start_edit(item_id: str) -> None:
    st.session_state["editing_id"] = item_id
    st.session_state["edit_warning"] = None


def _clear_edit_state(item_id: str) -> None:
    st.session_state["editing_id"] = None
    st.session_state["edit_warning"] = None
    st.session_state.pop(f"edit_name_{item_id}", None)
    st.session_state.pop(f"edit_qty_{item_id}", None)


def on_save_edit(item_id: str) -> None:
    name = st.session_state[f"edit_name_{item_id}"].strip()
    quantity = int(st.session_state[f"edit_qty_{item_id}"])
    if not name:
        st.session_state["edit_warning"] = "빈 이름으로는 저장할 수 없어요."
        return

    def change(items: list[dict]) -> str:
        item = find_item(items, item_id)
        if not item:
            return "⚠️ 다른 기기에서 삭제된 아이템이에요"
        item["name"] = name
        item["quantity"] = quantity
        return f"✏️ {toast_name(name)} 수정됨"

    mutate(change)
    _clear_edit_state(item_id)


def on_cancel_edit(item_id: str) -> None:
    _clear_edit_state(item_id)


def on_delete(item_id: str) -> None:
    def change(items: list[dict]) -> str | None:
        item = find_item(items, item_id)
        if not item:
            return None
        items.remove(item)
        return f"🗑️ {toast_name(item['name'])} 삭제됨"

    mutate(change)
    if st.session_state["editing_id"] == item_id:
        _clear_edit_state(item_id)


def on_delete_checked() -> None:
    def change(items: list[dict]) -> str:
        checked = [i for i in items if i["checked"]]
        for item in checked:
            items.remove(item)
        return f"🧹 체크된 항목 {len(checked)}개 삭제됨"

    mutate(change)  # 삭제된 아이템을 편집 중이었다면 render_item_list()가 편집 상태를 정리한다


# ---------- 화면 ----------

# 팔레트: 신선한 초록(메인) + 청록(그라데이션) + 오렌지(포인트).
# 배경/글자/입력창 색은 .streamlit/config.toml에 있다.
# Streamlit은 .stApp에 현재 테마의 color-scheme(light/dark)을 지정하므로, CSS light-dark()로
# 두 모드의 색을 함께 정의하면 테마가 바뀌는 즉시(스크립트 재실행 없이) 색이 따라 바뀐다.
CSS = """
<style>
.stApp {
    --hero: linear-gradient(135deg, light-dark(#16a34a, #15803d) 0%, light-dark(#0d9488, #0f766e) 100%);
    --hero-shadow: 0 12px 28px -14px light-dark(rgba(13, 148, 136, 0.65), rgba(0, 0, 0, 0.8));
    --card-bg: light-dark(#ffffff, #16251d);
    --card-border: light-dark(rgba(22, 163, 74, 0.14), rgba(34, 197, 94, 0.16));
    --card-shadow: 0 2px 10px -4px light-dark(rgba(29, 51, 41, 0.12), rgba(0, 0, 0, 0.5));
    --input-border: light-dark(rgba(29, 51, 41, 0.18), rgba(227, 241, 232, 0.16));
    --accent: light-dark(#16a34a, #22c55e);
    --done-accent: light-dark(#b9cbbf, #3b5246);
    --done-bg: light-dark(rgba(255, 255, 255, 0.55), rgba(22, 37, 29, 0.5));
    --checkbox-border: light-dark(#8fa898, #6b8576);
    --badge-bg: light-dark(#fff1e6, rgba(251, 146, 60, 0.16));
    --badge-fg: light-dark(#c2410c, #fdba74);
    --edit-hover: light-dark(rgba(22, 163, 74, 0.10), rgba(34, 197, 94, 0.14));
    --danger: light-dark(#dc2626, #f87171);
    --danger-soft: light-dark(rgba(220, 38, 38, 0.08), rgba(248, 113, 113, 0.10));
    --danger-border: light-dark(rgba(220, 38, 38, 0.30), rgba(248, 113, 113, 0.35));
}

/* 폼 안내 문구("Press Enter to submit form")가 좁은 화면에서 placeholder를 가리지 않게 숨김 */
[data-testid="InputInstructions"] { display: none; }

/* 화면에는 숨기고 스크린리더에는 읽히게 */
.sr-only, [class*="st-key-row_"] button [data-testid="stMarkdownContainer"] {
    position: absolute !important; width: 1px; height: 1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap;
}

/* 헤더: 그라데이션 배너 */
.st-key-header {
    background: var(--hero);
    box-shadow: var(--hero-shadow);
    border-radius: 1.25rem;
    padding: 1.4rem 1.5rem 1.2rem;
    gap: 0.25rem;
}
.st-key-header h1 { color: #ffffff; padding: 0; word-break: keep-all; }
.st-key-header [data-testid="stHeadingWithActionElements"] a { display: none; }
.st-key-header [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
.st-key-header .hero-sub { color: rgba(255, 255, 255, 0.88); margin: 0 0 0.9rem; }
.st-key-header [data-testid="stProgress"] p { color: #ffffff; font-weight: 600; }
.st-key-header [data-testid="stProgressBarTrack"] { background: rgba(255, 255, 255, 0.28); }
.st-key-header [data-testid="stProgressBarTrack"] > div { background: #ffffff; }
@media (max-width: 480px) {
    .st-key-header { padding: 1.1rem 1.2rem 1rem; }
    .st-key-header h1 { font-size: 2rem; }
}

/* 추가 버튼: 헤더와 같은 그라데이션 */
.st-key-add_row [data-testid^="stBaseButton"] { background: var(--hero); border: none; color: #ffffff; }
.st-key-add_row [data-testid^="stBaseButton"]:hover { filter: brightness(1.08); color: #ffffff; }

/* 입력창: 배경이나 카드 위에서도 경계가 보이도록 테두리와 그림자 */
.st-key-add_row [data-baseweb="input"],
.st-key-add_row [data-testid="stNumberInputContainer"],
[class*="st-key-editrow_"] [data-baseweb="input"],
[class*="st-key-editrow_"] [data-testid="stNumberInputContainer"] {
    border: 1px solid var(--input-border);
    border-radius: 0.6rem;
}
.st-key-add_row [data-baseweb="input"],
.st-key-add_row [data-testid="stNumberInputContainer"] { box-shadow: var(--card-shadow); }

/* 수량 −/+ 버튼도 손가락으로 누르기 쉬운 40px */
[data-testid="stNumberInputStepDown"],
[data-testid="stNumberInputStepUp"] { min-width: 2.5rem; min-height: 2.5rem; }

/* 공통: 행 안의 컬럼은 모바일에서도 줄바꿈하지 않음 */
.st-key-add_row [data-testid="stHorizontalBlock"],
[class*="st-key-editrow_"] [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    gap: 0.5rem;
}
.st-key-add_row [data-testid="stColumn"],
[class*="st-key-editrow_"] [data-testid="stColumn"] {
    min-width: 0 !important;
    width: auto !important;
}

/* 입력 폼: [이름(넓게) | 수량 | 추가] */
.st-key-add_row [data-testid="stColumn"]:nth-child(1) { flex: 1 1 0 !important; }
.st-key-add_row [data-testid="stColumn"]:nth-child(2) { flex: 0 0 8.5rem !important; }
.st-key-add_row [data-testid="stColumn"]:nth-child(3) { flex: 0 0 4.5rem !important; }
@media (max-width: 480px) {
    .st-key-add_row [data-testid="stColumn"]:nth-child(2) { flex: 0 0 4.5rem !important; }
    .st-key-add_row [data-testid="stColumn"]:nth-child(3) { flex: 0 0 4rem !important; }
}

/* 아이템 리스트 간격 */
.st-key-item_list { gap: 0.5rem; }

/* 아이템 카드: [체크박스 | 이름 x수량 | ✏️ | 🗑️] */
[class*="st-key-row_"],
[class*="st-key-editrow_"] {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-left: 4px solid var(--accent);
    border-radius: 0.85rem;
    box-shadow: var(--card-shadow);
    padding: 0.4rem 0.6rem 0.4rem 0.8rem;
    transition: background 0.2s, border-color 0.2s;
}
/* 아이템 줄(가로 컨테이너): 줄바꿈 없이 이름 칸만 늘어나고 줄어든다 */
[class*="st-key-row_"] { flex-wrap: nowrap !important; }
[class*="st-key-row_"] > [data-testid="stElementContainer"] { flex: 0 0 auto; }
[class*="st-key-row_"] > [data-testid="stElementContainer"]:has(.item-label) { flex: 1 1 0; min-width: 0; }
.item-label { margin: 0; overflow-wrap: anywhere; line-height: 1.6; }
.item-name { font-weight: 500; }
.item-qty {
    display: inline-block;
    margin-left: 0.5rem;
    padding: 0.05rem 0.5rem;
    border-radius: 999px;
    background: var(--badge-bg);
    color: var(--badge-fg);
    font-size: 0.8em;
    font-weight: 700;
    vertical-align: 0.1em;
}

/* 체크박스: 흰 카드 위에서도 잘 보이는 테두리 */
[class*="st-key-row_"] [data-testid="stCheckbox"] label > span:first-child { border-color: var(--checkbox-border); }

/* 아이콘 버튼: 작지만 손가락으로 누르기 쉬운 40px 이상 */
[class*="st-key-row_"] button {
    min-height: 2.5rem;
    min-width: 2.5rem;
    padding: 0 0.5rem;
    border-color: transparent;
    background: transparent;
    border-radius: 0.6rem;
}
[class*="st-key-row_"] [class*="st-key-edit_"] button:hover { background: var(--edit-hover); border-color: transparent; }
[class*="st-key-row_"] [class*="st-key-del_"] button:hover { background: var(--danger-soft); border-color: transparent; }

/* 체크된 아이템: 취소선 + 흐리게 + 회색 강조선 */
[class*="st-key-row_done_"] {
    background: var(--done-bg);
    border-left-color: var(--done-accent);
    box-shadow: none;
}
[class*="st-key-row_done_"] .item-name { text-decoration: line-through; }
[class*="st-key-row_done_"] .item-label { opacity: 0.5; }

/* 편집 중인 카드는 테두리 전체 강조 */
[class*="st-key-editrow_"] { border-color: var(--accent); }
[class*="st-key-editrow_"] [data-testid="stColumn"] { flex: 1 1 0 !important; }
[class*="st-key-editrow_"] [data-testid="stColumn"]:nth-child(1) { flex: 1.6 1 0 !important; }

/* 일괄 삭제 버튼: 부드러운 빨강 */
.st-key-delete_checked button:not(:disabled) {
    color: var(--danger);
    border-color: var(--danger-border);
    background: transparent;
}
.st-key-delete_checked button:not(:disabled):hover { background: var(--danger-soft); }

/* 자동 새로고침용 fragment는 공간을 차지하지 않게 */
.st-key-sync { display: none; }

/* 빈 리스트 안내 */
.empty-state { text-align: center; padding: 2.5rem 1rem; opacity: 0.6; }
.empty-state .icon { font-size: 2.5rem; line-height: 1; margin-bottom: 0.5rem; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def app_password() -> str | None:
    """배포 시 st.secrets에 APP_PASSWORD를 넣으면 비밀번호로 보호된다. 없으면 보호하지 않는다."""
    try:
        return st.secrets.get("APP_PASSWORD") or None
    except Exception:  # secrets.toml이 없을 때 Streamlit 버전에 따라 다른 예외가 난다
        return None


def check_password() -> bool:
    password = app_password()
    if not password or st.session_state["authed"]:
        return True
    with st.container(key="header"):
        st.title("🛒 쇼핑리스트")
        st.markdown('<p class="hero-sub">비밀번호를 입력해 주세요</p>', unsafe_allow_html=True)
    with st.form("login", border=False):
        entered = st.text_input("비밀번호", type="password")
        if st.form_submit_button("입장", type="primary", width="stretch"):
            if hmac.compare_digest(entered.encode(), str(password).encode()):
                st.session_state["authed"] = True
                st.rerun()
            st.error("비밀번호가 맞지 않아요.")
    return False


def render_header() -> None:
    items = st.session_state["items"]
    total = len(items)
    done = sum(i["checked"] for i in items)
    with st.container(key="header"):
        st.title("🛒 쇼핑리스트")
        if not total:
            sub = "오늘 장볼 목록을 담아보세요"
        elif done < total:
            sub = f"살 물건이 {total - done}개 남았어요"
        else:
            sub = "모두 담았어요!"
        st.markdown(f'<p class="hero-sub">{sub}</p>', unsafe_allow_html=True)
        st.progress(done / total if total else 0.0, text=f"{done} / {total} 완료")
    if total and done == total:
        st.success("🎉 장보기 완료!")


def render_messages() -> None:
    for key, show in (("error", st.error), ("notice", st.warning)):
        if st.session_state[key]:
            show(st.session_state[key])
            st.session_state[key] = None


def render_add_form() -> None:
    with st.form("add_form", clear_on_submit=True, border=False):
        with st.container(key="add_row"):
            col_name, col_qty, col_btn = st.columns(3, vertical_alignment="bottom")
            col_name.text_input("아이템 이름", key="new_name", placeholder="예: 우유")
            col_qty.number_input("수량", key="new_qty", min_value=1, value=1, step=1)
            col_btn.form_submit_button("추가", on_click=on_add, type="primary", width="stretch")
    if st.session_state["add_warning"]:
        st.warning(st.session_state["add_warning"])
        st.session_state["add_warning"] = None


def render_view_row(item: dict) -> None:
    item_id = item["id"]
    row_key = f"row_done_{item_id}" if item["checked"] else f"row_{item_id}"
    # st.columns 대신 가로 컨테이너를 쓴다. 한 줄의 요소가 10개에서 5개로 줄어 100개일 때도 빠르다.
    with st.container(key=row_key, horizontal=True, vertical_alignment="center", gap="small"):
        st.checkbox(
            f"구매 완료: {md_escape(item['name'])}",
            value=item["checked"],
            key=f"chk_{item_id}",
            on_change=on_toggle,
            args=(item_id,),
            label_visibility="collapsed",
            width="content",
        )
        # 이름은 마크다운을 거치지 않는 st.html로 출력한다 (HTML 이스케이프만 하면 되고, 렌더링도 가볍다)
        st.html(
            f'<p class="item-label"><span class="item-name">{html.escape(item["name"])}</span>'
            f'<span class="item-qty">x{item["quantity"]}</span></p>',
            width="stretch",
        )
        # 라벨 텍스트는 CSS로 숨겨 아이콘만 보이게 하고, 스크린리더는 "수정"/"삭제"를 읽는다
        st.button("수정", icon="✏️", key=f"edit_{item_id}",
                  on_click=on_start_edit, args=(item_id,), width="content")
        st.button("삭제", icon="🗑️", key=f"del_{item_id}",
                  on_click=on_delete, args=(item_id,), width="content")


def render_edit_row(item: dict) -> None:
    item_id = item["id"]
    with st.container(key=f"editrow_{item_id}"):
        st.text_input("이름", value=item["name"], key=f"edit_name_{item_id}")
        col_qty, col_save, col_cancel = st.columns(3, vertical_alignment="bottom")
        col_qty.number_input("수량", value=item["quantity"], min_value=1, step=1,
                             key=f"edit_qty_{item_id}")
        col_save.button("저장", key=f"save_{item_id}", on_click=on_save_edit,
                        args=(item_id,), type="primary", width="stretch")
        col_cancel.button("취소", key=f"cancel_{item_id}", on_click=on_cancel_edit,
                          args=(item_id,), width="stretch")
        if st.session_state["edit_warning"]:
            st.warning(st.session_state["edit_warning"])


def render_item_list() -> None:
    items = st.session_state["items"]
    if not items:
        st.markdown(
            '<div class="empty-state"><div class="icon">🧺</div>'
            "아직 추가된 아이템이 없어요<br>위에서 살 물건을 추가해 보세요</div>",
            unsafe_allow_html=True,
        )
        return
    editing = st.session_state["editing_id"]
    if editing and not find_item(items, editing):  # 다른 기기에서 삭제된 아이템을 편집 중이었음
        _clear_edit_state(editing)
        editing = None
    with st.container(key="item_list"):
        # 미체크 항목 위, 체크된 항목 아래 (각 그룹 안에서는 추가 순서 유지)
        for item in sorted(items, key=lambda i: i["checked"]):
            if editing == item["id"]:
                render_edit_row(item)
            else:
                render_view_row(item)


def render_footer() -> None:
    if not st.session_state["items"]:
        return
    has_checked = any(i["checked"] for i in st.session_state["items"])
    st.button(
        "🧹 체크된 항목 모두 삭제",
        key="delete_checked",
        on_click=on_delete_checked,
        disabled=not has_checked,
        width="stretch",
    )


@st.fragment(run_every=REFRESH_SECONDS)
def watch_for_changes() -> None:
    """다른 탭/기기에서 파일이 바뀌면 화면 전체를 다시 그린다. (파일 상태만 확인하므로 가볍다)"""
    with st.container(key="sync"):
        if file_signature() != st.session_state.get("data_sig"):
            st.rerun()


def show_toast() -> None:
    if st.session_state["toast"]:
        st.toast(st.session_state["toast"])
        st.session_state["toast"] = None


def main() -> None:
    st.set_page_config(page_title="쇼핑리스트", page_icon="🛒", layout="centered")
    init_state()
    inject_css()
    if not check_password():
        st.stop()
    refresh_items()
    show_toast()
    render_header()
    render_messages()
    render_add_form()
    render_item_list()
    render_footer()
    watch_for_changes()


if __name__ == "__main__":
    main()
