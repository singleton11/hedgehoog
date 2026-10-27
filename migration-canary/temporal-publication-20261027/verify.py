"""Isolated migration fixture: actual SQLite dedup, not product acceptance."""
import sqlite3
import time

db = sqlite3.connect(':memory:')
db.execute('create table events (id text primary key, effect text not null)')
for delivery in ('delivery-1', 'delivery-1', 'delivery-2'):
    db.execute('insert or ignore into events values (?, ?)', (delivery, 'committed'))
    print('Received:', delivery, '; durable distinct events:', db.execute('select count(*) from events').fetchone()[0], flush=True)
    time.sleep(0.3)
rows = db.execute('select id, effect from events order by id').fetchall()
assert rows == [('delivery-1', 'committed'), ('delivery-2', 'committed')], rows
print('Observed rows:', rows, flush=True)
print('ISOLATED MIGRATION FIXTURE; NOT PRODUCT ACCEPTANCE', flush=True)
