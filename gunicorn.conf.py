# gunicorn liest diese Datei automatisch (Render startet mit `gunicorn app:app`).
# Die KI-Analyse lädt Kurse und News und wartet dann auf Claude – das dauert
# oft länger als gunicorns Standard von 30 Sekunden, nach denen der Worker
# sonst abgebrochen wird (Fehler 502).
timeout = 180
