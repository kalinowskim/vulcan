# Zadanie: Vulcan Message Forwarder

Zbuduj mały serwis w Pythonie, który przekazuje mi na maila **pełną treść** wiadomości z e-dziennika Vulcan (eduVULCAN). Vulcan wysyła na maila tylko powiadomienie „masz nową wiadomość” bez treści. Pełna treść jest dostępna w darmowej wersji webowej. Serwis ma działać na moim VPS-ie.

## Jak to ma działać

1. Co N minut (domyślnie 5) serwis łączy się przez IMAP z dedykowaną skrzynką, na którą przekierowuję powiadomienia Vulcana.
2. Szuka nowych (nieprzetworzonych) maili z powiadomieniami Vulcana i parsuje z nich wszystko, co się da: nadawcę wiadomości, temat, datę, ucznia/dziennik, ewentualny link.
3. Jeśli są nowe powiadomienia, uruchamia Playwright (headless Chromium), loguje się na eduVULCAN moim kontem, przechodzi do modułu Wiadomości i znajduje wiadomość pasującą do powiadomienia.
4. Pobiera pełną treść (HTML i tekst), nadawcę, adresatów, datę i załączniki.
5. Wysyła to przez SMTP na mój docelowy adres jako czytelny mail HTML + wersja plain text, z załącznikami.
6. Oznacza powiadomienie jako przetworzone (flaga IMAP i/lub przeniesienie do folderu `Processed`) oraz zapisuje ID wiadomości Vulcana w SQLite, żeby nic nie poszło dwa razy.

## Stack i wymagania techniczne

- Python 3.12, Playwright (sync albo async, Twój wybór), `imaplib` lub `imapclient`, `smtplib` + `email.message`, SQLite, `pydantic-settings` do konfiguracji z `.env`.
- Struktura modułów: `config`, `imap_watcher`, `notification_parser`, `vulcan_client` (cała logika Playwright), `mailer`, `store` (SQLite), `main` (pętla / pojedynczy przebieg).
- Dwa tryby uruchamiania: `--once` (jeden przebieg, pod cron/systemd timer) oraz `--loop` (ciągła pętla z interwałem z konfiguracji).
- Tryb debug: `--headed` i `--debug`, które zapisują screenshoty i HTML strony do katalogu `debug/` na każdym kroku logowania i nawigacji.

## Konfiguracja (`.env`, dodaj `.env.example`)

```
IMAP_HOST=
IMAP_PORT=993
IMAP_USER=
IMAP_PASSWORD=
IMAP_FOLDER=INBOX
IMAP_PROCESSED_FOLDER=Processed

SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
MAIL_FROM=
MAIL_TO=

VULCAN_LOGIN=
VULCAN_PASSWORD=

POLL_INTERVAL_MINUTES=5
NOTIFICATION_SENDER_FILTER=   # adres/domena nadawcy powiadomień Vulcana
ALERT_EMAIL_TO=               # domyślnie = MAIL_TO
```

## Logowanie do Vulcana (Playwright)

- Logowanie przez eduVULCAN (login + hasło). **Nie zgaduj selektorów ani URL-i.** Najpierw uruchom przeglądarkę w trybie headed/debug, przejdź przez prawdziwy flow logowania i nawigacji do Wiadomości, a dopiero potem napisz selektory. Preferuj selektory po roli/tekście (`get_by_role`, `get_by_label`) zamiast kruchych klas CSS.
- Zapisuj sesję przez `storage_state` do pliku (`state/session.json`, chmod 600). Przy kolejnym przebiegu najpierw spróbuj użyć zapisanej sesji. Loguj się ponownie tylko gdy wygasła.
- Jeśli po drodze pojawi się captcha, 2FA albo nieoczekiwana strona: zrób screenshot, przerwij i wyślij mi mail alertowy z opisem i screenshotem. Nie próbuj obchodzić captchy.
- Jeśli moduł Wiadomości strony korzysta z wewnętrznych endpointów JSON, możesz po zalogowaniu przechwycić je przez `page.on("response")` albo wywołać przez `page.request` z tą samą sesją. To jest preferowane nad parsowaniem DOM, jeśli okaże się stabilniejsze. Zdecyduj po zbadaniu strony i opisz decyzję w README.
- Uwaga: otwarcie wiadomości w Vulcanie zwykle oznacza ją jako przeczytaną. To jest akceptowalne, ale opisz to w README.

## Dopasowanie powiadomienia do wiadomości

- Dopasuj po kombinacji: nadawca + temat + data/godzina (z tolerancją kilku minut).
- Fallback: jeśli nie da się jednoznacznie dopasować, pobierz wszystkie wiadomości odebrane nowsze niż ostatnio przetworzona (wg SQLite) i prześlij te, których jeszcze nie wysłano.
- Pierwsze uruchomienie: nie wysyłaj całej historii. Zapisz bieżące wiadomości jako „już widziane” (flaga `--init`).

## Mail wyjściowy

- Temat: `[Vulcan] <temat wiadomości> — <nadawca>`.
- W treści: nadawca, adresaci, data, uczeń, pełna treść, a na dole link do wiadomości w dzienniku (jeśli istnieje).
- Załączniki z Vulcana dołączone do maila. Jeśli pobranie się nie uda, link + informacja.
- Treść HTML z Vulcana zsanityzuj (usuń skrypty, zewnętrzne trackery), zachowaj formatowanie.

## Niezawodność i bezpieczeństwo

- Logowanie przez `logging` do stdout (pod journald/docker logs). **Nigdy nie loguj haseł, ciasteczek ani tokenów.**
- Retry z backoffem dla IMAP/SMTP/Playwright. Timeouty na każdym kroku.
- Alert mailowy przy błędzie, ale z throttlingiem (max 1 alert tego samego typu na godzinę), żeby nie zalać skrzynki.
- Lock pliku, żeby dwa przebiegi nie działały równolegle.
- `.env`, `state/`, `debug/` i baza w `.gitignore`.

## Deployment na VPS

Przygotuj oba warianty, opisz w README:

1. **Docker** (preferowany): `Dockerfile` na bazie oficjalnego obrazu `mcr.microsoft.com/playwright/python` w wersji zgodnej z biblioteką + `docker-compose.yml` z wolumenami na `state/`, bazę i `debug/`, `restart: unless-stopped`, tryb `--loop`.
2. **systemd**: venv + `playwright install --with-deps chromium`, unit `vulcan-forwarder.service` + `vulcan-forwarder.timer` uruchamiające `--once` co 5 minut.

## Testy

- Testy jednostkowe parsera powiadomień na fixture'ach `.eml` (dostarczę przykładowy mail z powiadomieniem, zapytaj mnie o niego, zanim napiszesz parser).
- Test dopasowania powiadomienie ↔ wiadomość.
- Test deduplikacji w SQLite.
- Komenda `--dry-run`: wszystko jak normalnie, ale zamiast wysyłać mail, wypisuje go na stdout.

## Kolejność pracy

1. Zapytaj mnie o przykładowy mail z powiadomieniem (.eml) i dane, których potrzebujesz.
2. Szkielet projektu, konfiguracja, IMAP watcher + parser z testami.
3. Playwright: logowanie i nawigacja w trybie headed/debug, zbadanie modułu Wiadomości, decyzja DOM vs JSON.
4. Pobieranie treści i załączników, dopasowanie, mailer, SQLite.
5. Obsługa błędów, alerty, lock.
6. Docker + systemd + README (instalacja krok po kroku, jak przetestować `--dry-run`, jak debugować po zmianie strony Vulcana).

Po każdym etapie krótko podsumuj, co działa, i poczekaj na moje OK przed kolejnym.
