---
name: factory
description: Uruchamiaj i nadzoruj lokalną Software Factory dla ticketu w bieżącym projekcie Codexa lub OpenCode. Używaj przy poleceniach $factory ID, /factory ID, factory status lub prośbie o przekazanie zadania fabryce.
---

# Factory w bieżącym projekcie

`$factory ID` oznacza przekazanie wskazanego ticketu fabryce w projekcie
otwartym w Codexie. `$factory status` pokazuje stan fabryki tego projektu.
W OpenCode odpowiedniki to `/factory ID` i `/factory status`; polecenie
instalowane jest z `integrations/opencode/commands/factory.md`. Oba klienty
korzystają z tego samego helpera i konfiguracji projektu. Wywołanie z OpenCode
nie zmienia runtime'u wykonawcy fabryki.
Bez argumentu sprawdź konfigurację i status; nie wybieraj sam ticketu.
Nie zakładaj, że `/factory` jest obsługiwane przez każdy klient Codexa.

Do wywołań używaj helpera `scripts/project.py` z tego skilla (Python 3.11+):

```sh
python scripts/project.py --project KATALOG --action status
python scripts/project.py --project KATALOG --action submit --task ID
```

Helper wybiera konfigurację tylko tego repozytorium i przekazuje dokładny ID
do komendy `submit`. Nie buduj własnego ręcznego enqueue. Dla wielu konfiguracji
przekaż wybraną ścieżkę przez `--config`. `--action run` uruchamia supervisor
na pierwszym planie; wykonuj to w trwałej sesji terminala i potwierdź gotowość.

## Wybierz projekt i instalację

1. Użyj bieżącego katalogu sesji, a nie repozytorium zawierającego ten skill.
   Odczytaj instrukcje projektu. Nie uruchamiaj automatycznie żadnego ticketu
   wymienionego wyłącznie w historii rozmowy.
2. Uruchom `scripts/project.py --project KATALOG` względnie do tego skilla,
   interpreterem Python 3.11+. Helper jest tylko do odczytu. Zwraca główne
   repozytorium projektu, katalog kodu fabryki i pasujące konfiguracje.
   Wskazana przez użytkownika konfiguracja może być sprawdzona przez `--config`.
3. Wybierz wyłącznie konfigurację, której repozytorium Git jest tym samym
   repozytorium co bieżący projekt (worktree tego samego repo też pasuje).
   Gdy pasuje kilka konfiguracji, rozstrzygnij z instrukcji projektu lub zapytaj
   o właściwą. Nigdy nie korzystaj z konfiguracji innego projektu jako zastępczej.
4. Gdy brak konfiguracji, przygotuj ją poleceniem `python -m factory --config
   CONFIG init --repo REPO --state-dir STATE` z katalogu kodu fabryki. Umieść
   prywatny stan poza repozytorium; ustaw quality gates z instrukcji tego
   projektu. Domyślne `make check` i `make e2e` są przykładem dla BuchOS,
   nie uniwersalnym kontraktem. Jeżeli projekt nie określa potrzebnych testów,
   ustal je przed uruchomieniem wykonawcy. Nie instaluj przy okazji usługi
   logowania, scannerów ani nowego runtime'u bez potrzeby wynikającej z prośby.

## Status i uruchomienie

Z katalogu kodu fabryki uruchom `python -m factory --config CONFIG status`.
Oceń gotowość, świeżość heartbeat, aktywne zadania, tracker i politykę integracji.
Istniejący supervisor pozostaje właścicielem wykonania. Nie startuj drugiego.
Jeśli supervisor nie działa, uruchom `python -m factory --config CONFIG run`
w trwałym terminalu lub istniejącym mechanizmie usług tego projektu, zachowując
możliwość inspekcji i zatrzymania. Obserwuj świeży, gotowy heartbeat i zgodną
tożsamość procesu przed ogłoszeniem startu. Herdr stosuj tylko na prośbę
użytkownika i zgodnie z jego skillem; nie jest warunkiem użycia fabryki.

## Ticket

Odczytaj wskazany ticket w trackerze bieżącego projektu. Aktualny adapter
obsługuje Beads; nie udawaj obsługi Jira/GitHub Issues bez adaptera. Sprawdź
zależności, właściciela, gotowość wymagań, zakres `factory_paths`, niezależność
względem aktywnej pracy i review oraz limit globalny. Zachowaj niezależne
review aktualnej rewizji. Nie przejmuj cudzej pracy.

Najpierw sprawdź, czy ticket ma już zadanie fabryki; wtedy pokaż jego stan lub
wznów je po usunięciu faktycznej blokady. Nie twórz drugiego wykonawcy.
Dla nowego zadania użyj `--action submit --task ID`. Komenda sprawdza gotowość
supervisora, projekt, etykiety `factory-pilot` i `ready-for-development`, scope,
zależności poprzez `bd ready`, limit aktywnej pracy i uwagi GitHub. Wywołanie
skilla autoryzuje wykonanie wskazanego zadania: po sprawdzeniu wymagań i zakresu
możesz dodać jego etykiety gotowości. Nie oznaczaj jako gotowego zadania z
nierozstrzygniętymi wymaganiami lub natywną blokadą. Zachowaj pełną akceptację.

Respektuj tryb projektu. `task_policy=source` dopuszcza kod. `source-review`
kończy zmiany źródłowe testami/review na branchu; `source-integrate` integruje
dokładną zatwierdzoną rewizję po zweryfikowanym checkpointcie kodu. Na prośbę
o zmianę trybu użyj `--action set-mode --mode source-review` lub
`source-integrate` (albo `documentation`). Nie usuwaj natywnych blokad ticketu
tylko dla uruchomienia. Integracja kodu nie uruchamia migracji ani aplikacji.
`source-integrate` wymaga skonfigurowanych `security_gates` i potwierdzonego
`integration_checkout_isolated: true`: działająca aplikacja/Vite nie może używać
checkoutu integracyjnego. Nie ustawiaj tej flagi bez sprawdzenia runtime'u.
Aktywacja to osobne `factory activate --revision SHA`, wymagające konfiguracji
`release`, z checkpointem stanu i kontrolą możliwości rollbacku.

Po przekazaniu potwierdź w statusie dokładny ticket, etap, worktree/branch i
żywego wykonawcę. Raportuj konkretną blokadę i następny krok, jeżeli start się
nie powiódł. Nie obiecuj pracy po zamknięciu sesji bez uruchomionego supervisora.
Wywołanie skilla nie rozszerza uprawnień do danych, push, merge czy deploymentu.
