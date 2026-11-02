# Vulcan Message Forwarder

Przekazuje na e-mail **pełną treść** wiadomości z dziennika VULCAN (eduVULCAN). Vulcan wysyła tylko
powiadomienie „masz nową wiadomość” — serwis łapie je w skrzynce IMAP, loguje się do eduVULCAN,
pobiera treść i wysyła czytelny mail (HTML + tekst) z nadawcą, adresatami, datą, uczniem i linkami
do załączników.

## Jak to działa

1. Co `POLL_INTERVAL_MINUTES` (domyślnie 5) serwis sprawdza IMAP (`IMAP_FOLDER`) w poszukiwaniu
   powiadomień od nadawców z `NOTIFICATION_SENDER_FILTER`.
2. Vulcan jest odpytywany, gdy przyszło powiadomienie **albo** minęło `FULL_CHECK_INTERVAL_MINUTES`
   (domyślnie 60) od ostatniego sprawdzenia — bo część powiadomień potrafi nie dojść.
3. Za każdym razem przekazywane są **wszystkie** wiadomości nowsze niż ostatnia znana (z dniem zapasu),
   których jeszcze nie wysłano. Powiadomienie w praktyce tylko przyspiesza sprawdzenie.
4. Powiadomienia są dopasowywane do wiadomości, żeby wiedzieć, kiedy przenieść je do `Processed`:
   - **po UUID** — linia `ID: …` w powiadomieniu to dokładnie `apiGlobalKey` wiadomości w Vulcanie;
   - bez UUID (np. zmieniony format): nadawca + temat + skrzynka ucznia + czas (±10 min).
5. Wysłane wiadomości są zapisywane w SQLite (`state/forwarder.db`) — nic nie pójdzie dwa razy.
   Powiadomienie dostaje flagę IMAP `$VulcanForwarded` i trafia do folderu `IMAP_PROCESSED_FOLDER`.

### Decyzje

- **JSON API zamiast parsowania strony.** Playwright tylko loguje się przez formularz eduVULCAN;
  wiadomości są pobierane z wewnętrznego API modułu Wiadomości
  (`wiadomosci.eduvulcan.pl/<symbol>/api/Odebrane`, `.../WiadomoscSzczegoly?apiGlobalKey=…`) w tej
  samej sesji przeglądarki. API zwraca kompletne dane (nadawca, wszyscy odbiorcy, data, HTML treści,
  załączniki), jest stabilniejsze niż układ strony, a jego klucz wiadomości jest tym samym UUID, który
  Vulcan wpisuje do powiadomienia.
- **Wiadomości zostają w Vulcanie nieprzeczytane.** Samo pobranie szczegółów przez API ich nie
  oznacza (w interfejsie robi to osobne żądanie przy otwarciu, którego serwis nie wysyła). Sprawdzone
  2026-11; jeśli Vulcan to zmieni, wiadomości zaczną być oznaczane jako przeczytane.
- **Załączniki jako linki.** Nauczyciele dołączają pliki jako linki OneDrive — mail zawiera nazwę
  pliku i link, pliki nie są pobierane.
- **Wiadomość do kilkorga dzieci** ma w Vulcanie osobną kopię (i osobny UUID) w skrzynce każdego
  ucznia, więc przychodzi osobno dla każdego (pole „Uczeń” w mailu).
- **Sesja** jest zapisywana w `state/session.json` (chmod 600) i używana ponownie; logowanie tylko,
  gdy wygaśnie. Na stronie logowania działa automatyczna ochrona przed botami, którą przeglądarka
  przechodzi sama. Jeśli pojawi się captcha do rozwiązania, 2FA lub nieznana strona — serwis robi
  zrzut ekranu, przerywa i wysyła alert. Logowanie nigdy nie jest ponawiane (ryzyko blokady konta).

### Niezawodność

- Ponawianie z backoffem (3 próby, 5 s → 15 s) dla IMAP, SMTP i API Vulcana — tylko dla błędów
  przejściowych (sieć, timeouty, 4xx SMTP, 5xx API).
- Alerty mailowe na `ALERT_EMAIL_TO` (domyślnie `MAIL_TO`), maks. 1 alert danego typu na godzinę:
  `vulcan-logowanie` (ze zrzutem ekranu), `vulcan`, `imap`, `smtp`, `siec`, `parser`, `dopasowanie`,
  `nieoczekiwany`.
- Blokada `state/forwarder.lock` — dwa przebiegi nie działają równolegle.
- Logi na stdout (journald / `docker logs`); hasła z konfiguracji są maskowane w każdym wpisie.

## Konfiguracja

Skopiuj `.env.example` do `.env` i uzupełnij. Najważniejsze:

| Zmienna | Opis |
|---|---|
| `IMAP_*` | skrzynka, do której trafiają powiadomienia Vulcana |
| `SMTP_*`, `MAIL_FROM` | skąd wysyłać (port 587 = STARTTLS, 465 = SSL) |
| `MAIL_TO` | dokąd przekazywać wiadomości; kilka adresów po przecinku |
| `VULCAN_LOGIN`, `VULCAN_PASSWORD` | konto eduVULCAN |
| `NOTIFICATION_SENDER_FILTER` | nadawcy powiadomień, np. `noreply@vulcan.net.pl`; kilka po przecinku |
| `ALERT_EMAIL_TO` | odbiorcy alertów; kilka po przecinku; puste = `MAIL_TO` |
| `POLL_INTERVAL_MINUTES` | co ile minut pętla sprawdza IMAP (tylko `--loop`) |
| `FULL_CHECK_INTERVAL_MINUTES` | co ile minut sprawdzać Vulcana bez powiadomienia (0 = wyłączone) |

Hasła ze znakami specjalnymi (`$`, `#`, spacja) wpisz w apostrofach: `VULCAN_PASSWORD='pa$$#word'`.

## Instalacja na VPS — Docker (zalecane)

Wymagania: Docker z pluginem compose (`docker compose version`).

```bash
# 1. Skopiuj projekt na serwer (z komputera, bez .venv/state/debug):
rsync -av --exclude .venv --exclude state --exclude debug --exclude .env \
    ./ user@vps:/opt/vulcan-forwarder/

# 2. Na serwerze:
cd /opt/vulcan-forwarder
cp .env.example .env && nano .env && chmod 600 .env
mkdir -p state debug && sudo chown -R 10001:10001 state debug   # UID użytkownika w kontenerze
docker compose build

# 3. Test (nic nie wysyła ani nie oznacza):
docker compose run --rm vulcan-forwarder --probe 3      # logowanie do Vulcana + 3 ostatnie wiadomości
docker compose run --rm vulcan-forwarder --once --dry-run

# 4. Pierwsze uruchomienie: oznacz obecne wiadomości jako widziane (bez tego nie wyśle historii,
#    ale okresowe sprawdzanie będzie wyłączone):
docker compose run --rm vulcan-forwarder --init

# 5. Start w tle (ciągła pętla, sam wstaje po restarcie serwera):
sudo systemctl enable --now docker
docker compose up -d
docker compose logs -f
```

`restart: unless-stopped` + włączony serwis `docker` = kontener startuje sam po restarcie VPS.

Aktualizacja: skopiuj nowe pliki, potem `docker compose up -d --build`. Przy zmianie wersji
`playwright` w `requirements.txt` zmień też tag obrazu w `Dockerfile`.

## Instalacja na VPS — systemd (bez Dockera)

Wymagania: Python 3.12+, Debian/Ubuntu.

```bash
sudo useradd --system --home-dir /opt/vulcan-forwarder --shell /usr/sbin/nologin vulcan
sudo mkdir -p /opt/vulcan-forwarder && sudo chown vulcan:vulcan /opt/vulcan-forwarder
# skopiuj pliki projektu do /opt/vulcan-forwarder (jak w kroku 1 dla Dockera), potem:
cd /opt/vulcan-forwarder
sudo -u vulcan python3 -m venv .venv
sudo -u vulcan .venv/bin/pip install -r requirements.txt
sudo .venv/bin/playwright install-deps chromium                       # biblioteki systemowe (root)
sudo -u vulcan PLAYWRIGHT_BROWSERS_PATH=/opt/vulcan-forwarder/.cache/ms-playwright \
    .venv/bin/playwright install chromium
sudo -u vulcan cp .env.example .env && sudo -u vulcan nano .env && sudo chmod 600 .env

# test i init
sudo -u vulcan PLAYWRIGHT_BROWSERS_PATH=/opt/vulcan-forwarder/.cache/ms-playwright \
    .venv/bin/python -m vulcan_forwarder --once --dry-run
sudo -u vulcan PLAYWRIGHT_BROWSERS_PATH=/opt/vulcan-forwarder/.cache/ms-playwright \
    .venv/bin/python -m vulcan_forwarder --init

# timer co 5 minut (--once), startuje sam po restarcie
sudo cp deploy/systemd/vulcan-forwarder.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vulcan-forwarder.timer
systemctl list-timers vulcan-forwarder.timer
journalctl -u vulcan-forwarder -f
```

## Tryby uruchamiania

| Komenda | Co robi |
|---|---|
| `--once` | jeden przebieg (timer systemd) |
| `--loop` | pętla co `POLL_INTERVAL_MINUTES` (Docker) |
| `--init` | zapisuje ostatnie ~200 wiadomości jako widziane, nic nie wysyła |
| `--dry-run` | z `--once`/`--loop`: maile na stdout zamiast SMTP; nic nie zapisuje w bazie ani w IMAP |
| `--sync` | z `--once`/`--loop`: wymusza pełne sprawdzenie Vulcana teraz |
| `--probe [N]` | diagnostyka: logowanie + N ostatnich wiadomości (niczego nie oznacza) |
| `--fetch-fixtures N` | zapisuje N ostatnich powiadomień z IMAP do `debug/eml/` |
| `--debug` | logi DEBUG + zrzuty ekranu/HTML każdego kroku i odpowiedzi API do `debug/run-*/` |
| `--headed` | przeglądarka z oknem (lokalnie, nie na serwerze bez ekranu) |

## Jak przetestować `--dry-run`

```bash
python -m vulcan_forwarder --once --dry-run          # powiadomienia z IMAP → mail na stdout
python -m vulcan_forwarder --once --sync --dry-run   # do tego pełne sprawdzenie Vulcana
```

Żeby zobaczyć efekt bez prawdziwego powiadomienia: przekaż (Gmail: „Przekaż”, albo jako załącznik)
dowolne stare powiadomienie Vulcana na skrzynkę IMAP z adresu dopisanego do
`NOTIFICATION_SENDER_FILTER` i uruchom `--once --dry-run`.

## Debugowanie po zmianie strony Vulcana

1. Sprawdź alert/log — typ `vulcan-logowanie` ma zrzut ekranu w załączniku i w `debug/run-*/`.
2. Uruchom `--probe 3 --debug` (lokalnie najlepiej także `--headed`). W `debug/run-<data>/` są
   zrzuty i HTML każdego kroku (`01_login-form`, `02_password-form`, `03_after-login`, `04_profiles`,
   `05_dziennik`, `06_messages-app`) i surowe odpowiedzi API (`*_api_*.json`).
3. Gdzie co poprawić (`vulcan_forwarder/vulcan_client.py`):
   - formularz logowania → `login()` (pole „Login”, przyciski „Dalej”/„Zaloguj”, `input[type=password]`),
     baner cookies → `_accept_cookies()` (iframe `#respect-privacy-frame`);
   - lista profili / symbol jednostki → `_detect_symbol()`; symbol jest zapamiętany w
     `state/vulcan.json` — usuń plik, żeby wykryć ponownie;
   - adresy i pola API → `MessageSummary.from_api`, `Message.from_api`, `list_received`, `get_message`.
     Aktualne nazwy endpointów podejrzysz w narzędziach deweloperskich przeglądarki (zakładka Sieć)
     po otwarciu modułu Wiadomości.
4. Zmiana formatu maila z powiadomieniem → alert `parser`; poprawki w
   `vulcan_forwarder/notification_parser.py`, nowy przykład do `tests/fixtures/` (`--fetch-fixtures`).
5. Wymuszenie ponownego logowania: usuń `state/session.json`.

## Rozwój

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/playwright install chromium
.venv/bin/pytest
```

Moduły: `config` (ustawienia), `imap_watcher` (IMAP), `notification_parser` (parser powiadomień),
`vulcan_client` (Playwright + API), `matcher` (dopasowanie), `mailer` (budowa i wysyłka maila,
sanityzacja HTML), `store` (SQLite), `alerts` (alerty z throttlingiem), `resilience` (retry, blokada,
maskowanie sekretów), `main` (CLI, przebieg).
