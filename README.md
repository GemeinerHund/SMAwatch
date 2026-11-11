# 200d-SMA-Wächter: S&P 500 & PHLX SOX

Läuft jeden US-Börsentag nach Handelsschluss (Mo–Fr 21:30 UTC) als GitHub Action:

- holt die Tagesschlusskurse (Yahoo Finance, Ersatzquelle Stooq)
- rechnet den 200-Tage-SMA für S&P 500 (`^GSPC`) und SOX (`^SOX`)
- schickt eine Push-Nachricht über **ntfy** aufs Handy
  - täglich: kurzer Status (über/unter, Abstand in %, seit wann)
  - bei einer **Kreuzung**: höchste Priorität mit 🚨
- aktualisiert die Status-Webseite (`docs/`, über GitHub Pages)

An Feiertagen ohne neuen Schlusskurs kommt keine Nachricht.

## Einrichtung (einmalig, ca. 5 Minuten)

1. **Repo**: öffentliches Repo anlegen und diese Dateien hineinlegen.
   (Öffentlich, weil GitHub Pages bei privaten Repos im Gratis-Plan nicht geht.
   Im Repo liegt nichts Geheimes – das ntfy-Topic ist ein Secret.)
2. **Secret**: Settings → Secrets and variables → Actions → *New repository secret*
   - Name: `NTFY_TOPIC`
   - Wert: dein Topic-Name (lang und zufällig, wie ein Passwort)
3. **Webseite**: Settings → Pages → *Deploy from a branch* → `main` / `/docs` → Save.
   Adresse danach: `https://<dein-user>.github.io/<repo-name>/`
   Tipp: auf dem Handy öffnen → Teilen → „Zum Home-Bildschirm“, dann wirkt sie wie eine App.
4. **Handy**: App **ntfy** installieren (iOS/Android, gratis) → „+“ → Topic
   `<dein-topic>` abonnieren.
5. **Test**: Actions → *200d SMA Watch* → *Run workflow*. Nach ca. 30 Sekunden
   kommt die erste Nachricht, die Webseite ist danach gefüllt.

## Optionen

- **Nur bei Kreuzungen benachrichtigen**: Settings → Secrets and variables →
  Actions → Reiter *Variables* → `ONLY_ON_CROSS` = `true`.
- **Uhrzeit ändern**: `cron` in `.github/workflows/sma-watch.yml` (Zeit in UTC).

## Hinweise

- Das Topic ist wie ein Passwort: wer es kennt, kann die Nachrichten mitlesen.
  Nicht öffentlich posten.
- GitHub startet geplante Läufe manchmal 5–30 Minuten später.
- Fällt ein Lauf aus, erkennt der nächste eine Kreuzung trotzdem (Vergleich mit
  dem zuletzt gespeicherten Stand).
- Schlägt der Datenabruf fehl, kommt eine Warn-Nachricht und GitHub schickt eine E-Mail.
