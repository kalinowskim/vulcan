"""Dostęp do modułu Wiadomości eduVULCAN przez Playwright.

Logowanie odbywa się przez UI (eduvulcan.pl), a wiadomości są pobierane z wewnętrznego
JSON API modułu Wiadomości (`wiadomosci.eduvulcan.pl/<symbol>/api/...`) w tej samej sesji
przeglądarki (`page.request`). Szczegóły i uzasadnienie: README, sekcja „Jak to działa”.

Zbadany przebieg (2026-11):
  1. https://eduvulcan.pl/logowanie — baner cookies w iframe #respect-privacy-frame („Zgadzam się”),
     pole „Login:” → „Dalej” → pole hasła → „Zaloguj”. W tle działa automatyczna ochrona
     przed botami („Zabezpieczenie przed robotami - proszę czekać”), która nie wymaga interakcji.
  2. https://eduvulcan.pl/dostep-do-dziennika/ — lista profili (uczniów); link profilu prowadzi
     przez SSO (dziennik-logowanie.vulcan.net.pl) do uczen.eduvulcan.pl/<symbol>/App/...
  3. https://wiadomosci.eduvulcan.pl/<symbol>/App — moduł Wiadomości, wszystkie skrzynki naraz.
     API: Skrzynki, Odebrane?idLastWiadomosc=&pageSize=, WiadomoscSzczegoly?apiGlobalKey=.
     Sam GET szczegółów NIE oznacza wiadomości jako przeczytanej (robi to osobny PUT z UI).
  4. https://uczen.eduvulcan.pl/<symbol>/api/... — dziennik ucznia (oceny, sprawdziany, zadania domowe),
     sesja z SSO po wejściu w profil; endpointy opisane w diary.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from vulcan_forwarder.config import Account, Settings
from vulcan_forwarder.diary import WARSAW, CalendarItem, Grade, Period, Student, parse_grades, periods_to_check
from vulcan_forwarder.resilience import retry

log = logging.getLogger(__name__)

LOGIN_URL = "https://eduvulcan.pl/logowanie"
PROFILES_URL = "https://eduvulcan.pl/dostep-do-dziennika/"
MESSAGES_HOST = "https://wiadomosci.eduvulcan.pl"
DIARY_HOST = "https://uczen.eduvulcan.pl"
STEP_TIMEOUT_MS = 30_000
NAV_TIMEOUT_MS = 60_000
PAGE_SIZE = 50


class VulcanError(RuntimeError):
    """Błąd po stronie Vulcana. `screenshot` wskazuje zrzut ekranu z chwili błędu (jeśli jest)."""

    def __init__(self, message: str, screenshot: Path | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class VulcanLoginError(VulcanError):
    """Logowanie się nie powiodło: złe dane, captcha do rozwiązania, 2FA albo nieoczekiwana strona."""


class VulcanApiError(VulcanError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def is_transient_vulcan_error(exc: BaseException) -> bool:
    """Timeouty i błędy sieci przeglądarki oraz 5xx z API — ponawiamy. Błędy logowania — nigdy
    (ryzyko blokady konta)."""
    if isinstance(exc, VulcanLoginError):
        return False
    if isinstance(exc, VulcanApiError):
        return exc.status >= 500 or exc.status == 429
    return isinstance(exc, PlaywrightError)


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str


@dataclass(frozen=True)
class MessageSummary:
    """Pozycja z listy odebranych (api/Odebrane)."""

    key: str  # apiGlobalKey — ten sam UUID co „ID:” w mailu z powiadomieniem
    id: int  # identyfikator wiadomości; wspólny dla kopii w kilku skrzynkach
    sender: str
    subject: str
    sent_at: datetime
    mailbox: str  # np. „Kowalski Adam - R - Kowalska Ola - (SP1)”
    has_attachments: bool
    read: bool

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> MessageSummary:
        return cls(
            key=item["apiGlobalKey"].lower(),
            id=int(item["id"]),
            sender=item.get("korespondenci") or "",
            subject=(item.get("temat") or "").strip(),
            sent_at=datetime.fromisoformat(item["data"]),
            mailbox=item.get("skrzynka") or "",
            has_attachments=bool(item.get("hasZalaczniki")),
            read=bool(item.get("przeczytana")),
        )


@dataclass(frozen=True)
class Message:
    """Pełna wiadomość (api/WiadomoscSzczegoly)."""

    key: str
    id: int
    sender: str
    recipients: list[str]
    subject: str
    sent_at: datetime
    html: str
    attachments: list[Attachment] = field(default_factory=list)
    withdrawn: bool = False
    mailbox: str | None = None  # z listy odebranych; szczegóły go nie zawierają

    @classmethod
    def from_api(cls, item: dict[str, Any], mailbox: str | None = None) -> Message:
        return cls(
            key=item["apiGlobalKey"].lower(),
            id=int(item["id"]),
            sender=item.get("nadawca") or "",
            recipients=list(item.get("odbiorcy") or []),
            subject=(item.get("temat") or "").strip(),
            sent_at=datetime.fromisoformat(item["data"]),
            html=item.get("tresc") or "",
            attachments=[
                Attachment(name=z.get("nazwaPliku") or "załącznik", url=z["url"])
                for z in item.get("zalaczniki") or []
                if z.get("url")
            ],
            withdrawn=bool(item.get("wycofana")),
            mailbox=mailbox,
        )


class VulcanClient:
    """Użycie: `with VulcanClient(settings) as v: v.list_received(...)`.

    Przy wejściu próbuje zapisanej sesji (state/session.json); loguje się ponownie tylko,
    gdy wygasła. Przy wyjściu zapisuje sesję z uprawnieniami 600.
    """

    def __init__(self, settings: Settings, account: Account, headed: bool = False, debug: bool = False):
        self.settings = settings
        self.account = account
        self.state_dir = account.state_dir(settings)
        self.headed = headed
        self.debug = debug
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._symbol: str | None = None
        self._step = 0
        self._debug_run_dir = settings.debug_dir / time.strftime(f"run-%Y%m%d-%H%M%S-{account.name}")

    # --- cykl życia -------------------------------------------------------------------------

    def __enter__(self) -> VulcanClient:
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=not self.headed)
            session = self.session_path
            self._ctx = self._browser.new_context(
                storage_state=str(session) if session.exists() else None,
                locale="pl-PL",
                timezone_id="Europe/Warsaw",
                viewport={"width": 1366, "height": 900},
            )
            self._ctx.set_default_timeout(STEP_TIMEOUT_MS)
            self._ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            self._page = self._ctx.new_page()
            self._symbol = self._load_meta().get("symbol")
            retry(self.ensure_session, what="Vulcan: sesja", should_retry=is_transient_vulcan_error, attempts=2)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._ctx is not None and exc_type is None:
                self._save_session()
        finally:
            for closer in (self._ctx, self._browser):
                if closer is not None:
                    try:
                        closer.close()
                    except PlaywrightError:
                        pass
            if self._pw is not None:
                self._pw.stop()
            self._pw = self._browser = self._ctx = self._page = None

    @property
    def page(self) -> Page:
        assert self._page is not None, "VulcanClient użyty poza blokiem with"
        return self._page

    # --- sesja i logowanie ------------------------------------------------------------------

    @property
    def messages_app_url(self) -> str:
        return f"{MESSAGES_HOST}/{self._symbol}/App"

    def _api_url(self, endpoint: str) -> str:
        return f"{MESSAGES_HOST}/{self._symbol}/api/{endpoint}"

    def ensure_session(self) -> None:
        if self._symbol and self._session_valid():
            log.info("Vulcan: zapisana sesja jest ważna")
            return
        log.info("Vulcan: brak ważnej sesji, logowanie")
        self.login()
        if not self._session_valid():
            raise VulcanLoginError("Po zalogowaniu API Wiadomości nadal niedostępne", self.snapshot("api-unavailable", force=True))

    def _session_valid(self) -> bool:
        """Otwiera moduł Wiadomości i sprawdza, czy API odpowiada JSON-em (a nie przekierowaniem do logowania)."""
        try:
            self.page.goto(self.messages_app_url, wait_until="domcontentloaded")
            self.snapshot("messages-app")
            if not self.page.url.startswith(MESSAGES_HOST):
                return False
            resp = self.page.request.get(self._api_url("Skrzynki"), max_redirects=0)
            return resp.ok and "json" in resp.headers.get("content-type", "")
        except PlaywrightError as exc:
            log.info("Vulcan: sprawdzenie sesji nie powiodło się: %s", str(exc).splitlines()[0])
            return False

    def login(self) -> None:
        page = self.page
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        self._accept_cookies()
        self.snapshot("login-form")

        page.get_by_label("Login").fill(self.account.login)
        page.get_by_role("button", name="Dalej").click()
        password = page.locator("input[type=password]")
        try:
            password.wait_for(state="visible")
        except PlaywrightTimeout:
            raise self._login_failure("Po podaniu loginu nie pojawiło się pole hasła") from None
        self.snapshot("password-form")

        password.fill(self.account.password.get_secret_value())
        page.get_by_role("button", name="Zaloguj").click()
        try:
            page.wait_for_url(lambda url: "/logowanie" not in url, timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout:
            raise self._login_failure("Logowanie nie przeszło dalej niż formularz") from None
        page.wait_for_load_state("domcontentloaded")
        self.snapshot("after-login")
        if page.get_by_role("link", name="Wylogowanie").count() == 0:
            raise self._login_failure(f"Po zalogowaniu nieoczekiwana strona: {page.url}")
        log.info("Vulcan: zalogowano")

        if not self._symbol:
            self._symbol = self._detect_symbol()
            self._save_meta({**self._load_meta(), "symbol": self._symbol})

    def _accept_cookies(self) -> None:
        button = self.page.frame_locator("#respect-privacy-frame").get_by_role("button", name="Zgadzam się")
        try:
            button.click(timeout=5_000)
            log.debug("Vulcan: zaakceptowano baner cookies")
        except PlaywrightTimeout:
            pass  # baner nie pojawił się (zgoda już zapisana w sesji)

    def _detect_symbol(self) -> str:
        """Symbol jednostki (np. „krakow”) z adresu dziennika po wejściu w pierwszy profil."""
        page = self.page
        page.goto(PROFILES_URL, wait_until="domcontentloaded")
        self.snapshot("profiles")
        profile = page.locator("a[href*='/dziennik?']").first
        try:
            profile.click()
            page.wait_for_url(re.compile(r"https://uczen\.eduvulcan\.pl/[^/]+/App"), timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout:
            raise self._login_failure("Nie udało się wejść do dziennika z listy profili") from None
        self.snapshot("dziennik")
        symbol = page.url.split("/")[3]
        log.info("Vulcan: symbol jednostki: %s", symbol)
        return symbol

    def _login_failure(self, reason: str) -> VulcanLoginError:
        page = self.page
        shot = self.snapshot("login-failure", force=True)
        details = []
        for sel in ("#localMessage", "#localMessage2", ".validation-summary-errors", ".message-error"):
            loc = page.locator(sel)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    text = loc.nth(i).inner_text().strip()
                    if text:
                        details.append(text)
        body = page.locator("body").inner_text(timeout=5_000).lower() if page.url else ""
        if any(word in body for word in ("przepisz kod", "rozwiąż", "jestem robotem", "recaptcha", "hcaptcha")):
            details.append("Wygląda na captchę do rozwiązania przez człowieka")
        if any(word in body for word in ("kod weryfikacyjny", "uwierzytelnianie dwuskładnikowe", "jednorazowy kod")):
            details.append("Wygląda na 2FA")
        message = reason + (f" ({'; '.join(details)})" if details else "") + f" [url: {page.url}]"
        return VulcanLoginError(message, shot)

    def _save_session(self) -> None:
        path = self.session_path
        path.parent.mkdir(parents=True, exist_ok=True)
        assert self._ctx is not None
        self._ctx.storage_state(path=str(path))
        _chmod_600(path)

    @property
    def _meta_path(self) -> Path:
        return self.state_dir / "vulcan.json"

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    def _load_meta(self) -> dict[str, Any]:
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_meta(self, meta: dict[str, Any]) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(json.dumps(meta), encoding="utf-8")

    # --- API wiadomości --------------------------------------------------------------------

    def _get_json(self, endpoint: str, host: str = MESSAGES_HOST) -> Any:
        name = endpoint.split("?")[0]
        return retry(lambda: self._get_json_once(endpoint, host), what=f"Vulcan: API {name}",
                     should_retry=is_transient_vulcan_error)  # fmt: skip

    def _get_json_once(self, endpoint: str, host: str = MESSAGES_HOST) -> Any:
        url = f"{host}/{self._symbol}/api/{endpoint}"
        resp = self.page.request.get(url, max_redirects=0, timeout=STEP_TIMEOUT_MS)
        if not resp.ok or "json" not in resp.headers.get("content-type", ""):
            raise VulcanApiError(
                f"API {endpoint.split('?')[0]}: HTTP {resp.status} ({resp.headers.get('content-type')})", resp.status
            )
        data = resp.json()
        if self.debug:
            self._dump_json(endpoint, data)
        return data

    def mailboxes(self) -> list[str]:
        """Nazwy skrzynek konta (np. „Rodzic - R - Uczeń - (Szkoła)”). Zapamiętywane w state, żeby
        przypisywać powiadomienia do kont bez logowania (`known_mailboxes`)."""
        names = [box.get("nazwa") or "" for box in self._get_json("Skrzynki")]
        self._save_meta({**self._load_meta(), "mailboxes": names})
        return names

    def list_received(self, stop_before: datetime | None = None, max_pages: int = 20) -> list[MessageSummary]:
        """Odebrane, od najnowszych. Kończy, gdy dojdzie do wiadomości starszych niż `stop_before`."""
        result: list[MessageSummary] = []
        last_id = 0
        for _ in range(max_pages):
            batch = self._get_json(f"Odebrane?idLastWiadomosc={last_id}&pageSize={PAGE_SIZE}")
            if not batch:
                break
            items = [MessageSummary.from_api(i) for i in batch]
            # Kursor `id` jest wspólny dla kopii wiadomości w kilku skrzynkach, więc strony
            # mogą się zazębiać — deduplikujemy po kluczu.
            seen = {m.key for m in result}
            result.extend(m for m in items if m.key not in seen)
            last_id = items[-1].id
            if stop_before is not None and items[-1].sent_at < stop_before:
                break
            if len(batch) < PAGE_SIZE:
                break
        if stop_before is not None:
            result = [m for m in result if m.sent_at >= stop_before]
        return result

    def get_message(self, key: str, mailbox: str | None = None) -> Message:
        """Pełna treść wiadomości. Nie oznacza jej jako przeczytanej."""
        return Message.from_api(self._get_json(f"WiadomoscSzczegoly?apiGlobalKey={key}"), mailbox=mailbox)

    # --- dziennik ucznia (oceny, sprawdziany, zadania domowe) ---------------------------------

    def diary_url(self, student_key: str, route: str) -> str:
        return f"{DIARY_HOST}/{self._symbol}/App/{student_key}/{route}"

    def _diary_session_valid(self) -> bool:
        try:
            resp = self.page.request.get(f"{DIARY_HOST}/{self._symbol}/api/Context", max_redirects=0)
            return resp.ok and "json" in resp.headers.get("content-type", "")
        except PlaywrightError as exc:
            log.info("Vulcan: sprawdzenie sesji dziennika nie powiodło się: %s", str(exc).splitlines()[0])
            return False

    def enter_diary(self) -> None:
        """Sesja w uczen.eduvulcan.pl. Gdy wygasła — wejście przez listę profili (SSO z eduvulcan.pl)."""
        if self._diary_session_valid():
            return
        log.info("Vulcan: wejście do dziennika ucznia przez listę profili")
        page = self.page
        page.goto(PROFILES_URL, wait_until="domcontentloaded")
        self.snapshot("profiles")
        try:
            page.locator("a[href*='/dziennik?']").first.click()
            page.wait_for_url(re.compile(rf"{re.escape(DIARY_HOST)}/[^/]+/App"), timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout:
            raise self._login_failure("Nie udało się wejść do dziennika ucznia z listy profili") from None
        self.snapshot("dziennik")
        if not self._diary_session_valid():
            raise VulcanError("Po wejściu do dziennika API ucznia nadal niedostępne", self.snapshot("diary-api", force=True))

    def students(self) -> list[Student]:
        """Wszyscy uczniowie konta (api/Context dziennika)."""
        return [Student.from_api(s) for s in self._get_json("Context", DIARY_HOST).get("uczniowie") or []]

    def grades(self, student: Student, today: date) -> list[Grade]:
        """Oceny cząstkowe ucznia z bieżącego (i tuż po zmianie — poprzedniego) okresu klasyfikacyjnego."""
        periods = [Period.from_api(p) for p in
                   self._get_json(f"OkresyKlasyfikacyjne?key={student.key}&idDziennik={student.diary_id}", DIARY_HOST)]  # fmt: skip
        result: list[Grade] = []
        for period in periods_to_check(periods, today):
            data = self._get_json(f"Oceny?key={student.key}&idOkresKlasyfikacyjny={period.id}", DIARY_HOST)
            result.extend(parse_grades(data, student.key))
        return result

    def calendar(self, student: Student, start: date, end: date) -> list[CalendarItem]:
        """Sprawdziany, kartkówki i zadania domowe z terminem od `start` do `end` włącznie."""
        since = datetime.combine(start, datetime.min.time(), WARSAW).astimezone(timezone.utc)
        until = datetime.combine(end + timedelta(days=1), datetime.min.time(), WARSAW).astimezone(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        data = self._get_json(f"SprawdzianyZadaniaDomowe?key={student.key}&dataOd={since:{fmt}}&dataDo={until:{fmt}}",
                              DIARY_HOST)  # fmt: skip
        return [CalendarItem.from_api(i, student.key) for i in data or []]

    def calendar_details(self, item: CalendarItem) -> dict[str, Any]:
        """Opis i nauczyciel sprawdzianu / zadania domowego."""
        endpoint = "ZadanieDomoweSzczegoly" if item.is_homework else "SprawdzianSzczegoly"
        return self._get_json(f"{endpoint}?key={item.student}&id={item.id}", DIARY_HOST)

    # --- debug -------------------------------------------------------------------------------

    def snapshot(self, name: str, force: bool = False) -> Path | None:
        """W trybie --debug (lub gdy force) zapisuje screenshot i HTML strony."""
        if not (self.debug or force) or self._page is None:
            return None
        self._step += 1
        out = self._debug_run_dir
        out.mkdir(parents=True, exist_ok=True)
        base = out / f"{self._step:02d}_{name}"
        try:
            self._page.screenshot(path=str(base.with_suffix(".png")))
            (base.with_suffix(".html")).write_text(self._redact(self._page.content()), encoding="utf-8")
        except PlaywrightError as exc:
            log.warning("Nie udało się zapisać zrzutu %s: %s", name, str(exc).splitlines()[0])
            return None
        log.debug("Zrzut: %s.png (%s)", base, self._page.url)
        return base.with_suffix(".png")

    def _dump_json(self, endpoint: str, data: Any) -> None:
        self._step += 1
        out = self._debug_run_dir
        out.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9]+", "_", endpoint)[:60]
        (out / f"{self._step:02d}_api_{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def _redact(self, text: str) -> str:
        secret = self.account.password.get_secret_value()
        return text.replace(secret, "***") if secret else text


def known_mailboxes(settings: Settings, account: Account) -> list[str]:
    """Skrzynki konta zapamiętane przy ostatnim połączeniu (pusta lista, gdy jeszcze nie znane)."""
    try:
        meta = json.loads((account.state_dir(settings) / "vulcan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(meta.get("mailboxes") or [])


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        log.debug("chmod 600 nie powiódł się dla %s", path)
