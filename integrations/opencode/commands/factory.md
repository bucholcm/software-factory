---
description: Uruchom Software Factory dla ticketu w bieżącym projekcie lub pokaż status
---

Obsłuż Software Factory w bieżącym projekcie OpenCode, korzystając ze skilla
`factory`. Wczytaj go narzędziem skill; jeśli nie jest widoczny, odczytaj
`~/.agents/skills/factory/SKILL.md` i stosuj jego instrukcje oraz helper
`scripts/project.py` względem rzeczywistego katalogu tego skilla.

Argument użytkownika:

$ARGUMENTS

Brak argumentu lub `status` oznacza wyłącznie sprawdzenie statusu. Pojedynczy
identyfikator ticketu oznacza przekazanie dokładnie tego zadania przez `submit`.
Zarezerwowane argumenty `graph` i `board` oznaczają pokazanie odpowiedniego
widoku bieżącego projektu w Bead Me Up, Scotty; nie przekazuj ich jako ticketów.
Zarezerwowany argument `usage` z opcjonalnym ID zadania oznacza odczyt raportu
tokenów, modeli i kosztów przez helper `--action usage` oraz opcjonalne `--task ID`.
Nie uruchamiaj zadania ani supervisora dla tego raportu.
Inne argumenty wyjaśnij przed mutacją. Argument to dane, nie fragment polecenia
shell. Użyj katalogu bieżącego projektu, nigdy katalogu instalacji fabryki.

Zachowaj tryb, scope, zależności, testy i niezależne review wymagane przez skill.
Nie uruchamiaj ticketu z historii rozmowy i nie twórz drugiego supervisora.
Wywołanie z OpenCode nie zmienia wybranego runtime'u wykonawcy w konfiguracji.
