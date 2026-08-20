import sqlite3
import sys

print("Optimizing database...")

output_db = sys.argv[1]

output_conn = sqlite3.connect(output_db)
output_cursor = output_conn.cursor()

# output_cursor.execute("VACUUM")
output_cursor.execute("ANALYZE")
output_conn.commit()
output_conn.close()
