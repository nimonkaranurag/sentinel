PY ?= uv run python
PYTEST ?= uv run pytest
DB ?= ledger.db

.PHONY: init hooks backfill categorize relink report poll notify plan digest backup test

# Cron (Europe/Dublin). `poll` is the one ingest path — it also categorizes and
# fires policy alerts, so there is no alert-less ingest target to run by mistake.
#   45 7,12,17,21 * * *  make poll     # ingest + categorize + policy alerts
#   0  8 * * *           make notify   # daily safe-to-spend + answer commands
#   0  8 * * 1           make plan     # Monday weekly plan
#   0 18 * * 0           make digest   # Sunday weekly report
#   30 2 * * *           make backup   # nightly sqlite .backup

init:
	mkdir -p data/backfill reports backups
	$(PY) -m sentinel.db --init

hooks:  ## install the pre-commit hook (secrets + PII scan)
	sh scripts/install-hooks.sh

backfill:
	$(PY) -m sentinel.csv_import data/backfill/*.csv

categorize:
	$(PY) -m sentinel.categorize

relink:  ## rebuild merchant links after a normalizer/rule change (atomic)
	$(PY) -m sentinel.categorize --relink

report:
	$(PY) -m sentinel.reports

poll:  ## ingest + categorize + policy alerts (the cron path; consumes 1 API unit)
	$(PY) -m sentinel.notify --poll

notify:  ## daily safe-to-spend push + answer pending commands
	$(PY) -m sentinel.notify

plan:  ## Monday weekly plan push (idempotent per ISO week)
	$(PY) -m sentinel.notify --plan

digest:  ## Sunday weekly digest (deterministic template)
	$(PY) -m sentinel.notify --digest

backup:
	mkdir -p backups
	sqlite3 $(DB) ".backup 'backups/ledger-`date +%F`.db'"

test:
	$(PYTEST) -q
