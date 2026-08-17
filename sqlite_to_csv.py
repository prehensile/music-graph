import sqlite3
import click
import csv
import os

from progress import Heartbeat

@click.command()
@click.option('--output-folder', required=True, type=click.Path(), help='Output folder for CSV files')
@click.option('--sqlite-path', required=True, type=click.Path(), help='Path to SQLite database file')
def main(output_folder, sqlite_path):
    
    os.makedirs(output_folder,exist_ok=True)

    fn_csv = f"{output_folder}/labels.csv"
    writer = csv.writer( open(fn_csv,"w"), quoting=csv.QUOTE_STRINGS )
    writer.writerow( ["labelId:ID(Label)", "name", "profile"] )

    conn = sqlite3.connect(sqlite_path)
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM labels')
    total_rows = cursor.fetchone()[0]

    beat = Heartbeat("labels", total=total_rows, unit="labels")
    beat.begin(f"{total_rows:,} rows -> {fn_csv}")

    cursor.execute('SELECT * FROM labels')
    for row in cursor:
        writer.writerow(row)
        beat.tick()

    beat.finish()
    conn.close()

if __name__ == "__main__":
    main()