#!/usr/bin/env python3

import sqlite3
import time
import os
from xml.etree import ElementTree
from alive_progress import alive_bar
import click

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
    start_time = time.time()
    
    with open(xml_file, "rb") as fp_xml:
        context = ElementTree.iterparse(
            fp_xml,
            events=["end"]
        )
        
        num_records = 0
        batch_size = 1000
        batch_data = []
        
        with alive_bar(dual_line=True, receipt_text=True) as bar:
            for event, element in context:
                
                if element.tag == "release":
                    release_id = element.attrib["id"]
                    content = ElementTree.tostring(element, encoding='unicode')
                    
                    batch_data.append((int(release_id), content))
                    num_records += 1
                    
                    # Insert in batches for better performance
                    if len(batch_data) >= batch_size:
                        cursor.executemany(
                            "INSERT OR REPLACE INTO releases (id, content) VALUES (?, ?)",
                            batch_data
                        )
                        conn.commit()
                        batch_data = []
                    
                    element.clear()
                
                current_pos = fp_xml.tell()
                pc = (current_pos / file_size) * 100
                
                # Calculate ETA
                elapsed_time = time.time() - start_time
                if pc > 0 and elapsed_time > 0:
                    bytes_per_second = current_pos / elapsed_time
                    remaining_bytes = file_size - current_pos
                    eta_seconds = remaining_bytes / bytes_per_second
                    
                    if eta_seconds < 60:
                        eta_str = f"{eta_seconds:.0f}s"
                    elif eta_seconds < 3600:
                        eta_str = f"{eta_seconds//60:.0f}m {eta_seconds%60:.0f}s"
                    else:
                        eta_str = f"{eta_seconds//3600:.0f}h {(eta_seconds%3600)//60:.0f}m"
                    
                    bar.text(f"{pc:.2f}% of file (ETA: {eta_str})")
                else:
                    bar.text(f"{pc:.2f}% of file")
                bar()
        
        # Insert remaining batch
        if batch_data:
            cursor.executemany(
                "INSERT OR REPLACE INTO releases (id, content) VALUES (?, ?)",
                batch_data
            )
            conn.commit()
        
        print(f"Processed {num_records:,} releases")
    
    conn.close()

@click.command()
@click.option('--xml-file', required=True, type=click.Path(exists=True), help='Path to releases XML file')
@click.option('--db-path', required=True, type=click.Path(), help='Path to SQLite database file')
def main(xml_file, db_path):
    parse_releases_xml(xml_file, db_path)

if __name__ == "__main__":
    main()