import sqlite3
import click
import csv
from alive_progress import alive_bar
import os

@click.command()
@click.option('--output-folder', required=True, type=click.Path(), help='Output folder for CSV files')
@click.option('--sqlite-path', required=True, type=click.Path(), help='Path to SQLite database file')
def main(output_folder, sqlite_path):
    
    os.makedirs(output_folder,exist_ok=True)

    fn_csv = f"{output_folder}/labels.csv"
    writer = csv.writer( open(fn_csv,"w"), quoting=csv.QUOTE_STRINGS )
    writer.writerow( ["labelId:ID(Label)", "year", "title"] )

    conn = sqlite3.connect(sqlite_path)
    cursor = conn.cursor()

    # First, get the total count for the progress bar
    cursor.execute('SELECT COUNT(*) FROM labels')
    total_rows = cursor.fetchone()[0]

    # Now execute the main query
    cursor.execute('SELECT * FROM labels')

    # Wrap the iteration in alive_bar
    with alive_bar(total_rows, title="Processing labels") as bar:
        for row in cursor:
            writer.writerow(row)
            bar()  # Update progress bar

    conn.close()

if __name__ == "__main__":
    main()