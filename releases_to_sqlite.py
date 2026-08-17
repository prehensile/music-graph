#!/usr/bin/env python3

import sqlite3
import os
from xml.etree import ElementTree
import click

from progress import Heartbeat

def create_database(db_path):
    """Create SQLite database with releases table"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY,
            content TEXT
        )
    ''')
    
    conn.commit()
    return conn

def parse_releases_xml(xml_file, db_path):
    """Parse releases XML file and insert into SQLite database"""
    
    conn = create_database(db_path)
    cursor = conn.cursor()
    
    file_size = os.path.getsize(xml_file)
    beat = Heartbeat("index", total=file_size, unit="releases")
    beat.begin(os.path.basename(xml_file))

    with open(xml_file, "rb") as fp_xml:
        context = ElementTree.iterparse(
            fp_xml,
            events=["end"]
        )

        batch_size = 1000
        batch_data = []

        for event, element in context:

            if element.tag != "release":
                continue

            release_id = element.attrib["id"]
            content = ElementTree.tostring(element, encoding='unicode')

            batch_data.append((int(release_id), content))

            # Insert in batches for better performance
            if len(batch_data) >= batch_size:
                cursor.executemany(
                    "INSERT OR REPLACE INTO releases (id, content) VALUES (?, ?)",
                    batch_data
                )
                conn.commit()
                batch_data = []

            element.clear()
            beat.tick(position=fp_xml.tell())

        # Insert remaining batch
        if batch_data:
            cursor.executemany(
                "INSERT OR REPLACE INTO releases (id, content) VALUES (?, ?)",
                batch_data
            )
            conn.commit()

    beat.finish()
    print(f"Processed {beat.count:,} releases")
    conn.close()

@click.command()
@click.option('--xml-file', required=True, type=click.Path(exists=True), help='Path to releases XML file')
@click.option('--db-path', required=True, type=click.Path(), help='Path to SQLite database file')
def main(xml_file, db_path):
    parse_releases_xml(xml_file, db_path)

if __name__ == "__main__":
    main()