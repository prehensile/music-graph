import csv
import sys

fn_csv = sys.argv[1]

all_ids = set()
dupe_indexes = {}
row_indexes = {}

with open( fn_csv ) as csvfile:
    reader = csv.reader( csvfile )
    next(reader)
    for idx, row in enumerate( reader ):
        id = row[0]
        if id in row_indexes:
            dupes = []
            if id in dupe_indexes:
                dupes = dupe_indexes[id]
            dupes.append(idx)
            dupe_indexes[id] = dupes
        else:
            row_indexes[id] = idx
            

print( f"{len(dupe_indexes.keys())} dupes)" )

for dupe_key in dupe_indexes:
    dupe_lines = dupe_indexes[dupe_key]
    print(
        f"Dupes for id {dupe_key} found on lines {dupe_lines}"
    )