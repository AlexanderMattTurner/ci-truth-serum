"""Keep only this shard's slice of a cosmic-ray session's work items.

Usage: prune-mutation-session.py SESSION_DB TOTAL INDEX

Deletes every work item whose rowid is not congruent to INDEX modulo TOTAL.
"""

import sqlite3
import sys

db, total, index = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
conn = sqlite3.connect(db)
conn.execute("DELETE FROM work_items WHERE rowid % ? != ?", (total, index))
conn.commit()
conn.close()
