# Migrations

Numbered `00N_*.sql`, applied in order by `sentinel.db.init_db()`, tracked via
`PRAGMA user_version`. Version 1 is `../schema.sql` (applied to fresh
databases); the first migration file here must therefore be `002_*.sql`.
